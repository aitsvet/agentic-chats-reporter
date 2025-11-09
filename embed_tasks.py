#!/usr/bin/env python3
import sqlite3
import argparse
import os
import base64
import gzip
import json
import sys
import time
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI
from task_builder import TaskBuilder
from llm_utils import get_model_context_size, tokens_to_chars


class TaskEmbedder:
    def __init__(self, chats_db: str, emb_url: str, emb_model: str, emb_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        self.emb_model = emb_model
        api_key = emb_api_key or os.getenv('EMB_API_KEY', 'not-needed')
        self.client = OpenAI(base_url=emb_url, api_key=api_key)
        self.task_builder = TaskBuilder(chats_db)
        context_size_tokens = self._get_model_context_size()
        self.context_size_chars = tokens_to_chars(context_size_tokens)
        self._create_tables()
    
    def _create_tables(self):
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_embeddings (
                user_msg_id INTEGER PRIMARY KEY,
                embedding_data TEXT,
                message_count INTEGER,
                formatted_length INTEGER,
                FOREIGN KEY (user_msg_id) REFERENCES messages(id)
            )
        """)
        
        # Add formatted_length column if it doesn't exist
        try:
            self.chats_cursor.execute("ALTER TABLE task_embeddings ADD COLUMN formatted_length INTEGER")
        except:
            pass  # Column already exists
        
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                threshold REAL,
                group_id INTEGER,
                user_msg_id INTEGER,
                FOREIGN KEY (user_msg_id) REFERENCES messages(id)
            )
        """)
        
        self.chats_cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_threshold ON task_groups(threshold, group_id)")
        self.chats_conn.commit()
    
    def _get_model_context_size(self) -> int:
        return get_model_context_size(self.client, self.emb_model, model_type='emb')
    
    def _aggressive_deduplicate_summaries(self, summaries: List[str]) -> List[str]:
        """Apply maximum deduplication to summaries using progressive levels."""
        if not summaries:
            return []
        
        dedup_level = 1
        filtered = summaries[:]
        
        while dedup_level <= 5:
            if dedup_level == 1:
                seen = set()
                filtered = []
                for s in summaries:
                    normalized = self.task_builder._normalize_summary(s)
                    if normalized not in seen:
                        seen.add(normalized)
                        filtered.append(s)
            
            elif dedup_level == 2:
                seen_window = {}
                filtered = []
                for i, s in enumerate(summaries):
                    normalized = self.task_builder._normalize_summary(s)
                    if normalized not in seen_window or i - seen_window[normalized] > 3:
                        seen_window[normalized] = i
                        filtered.append(s)
            
            elif dedup_level == 3:
                file_ops = {}
                filtered = []
                for s in summaries:
                    normalized = self.task_builder._normalize_summary(s)
                    is_file_op = 'Read file:' in s or 'Edit file:' in s
                    if is_file_op:
                        if normalized not in file_ops:
                            file_ops[normalized] = []
                        file_ops[normalized].append(s)
                    else:
                        filtered.append(s)
                for normalized, ops in file_ops.items():
                    if ops:
                        filtered.append(ops[-1])
            
            elif dedup_level == 4:
                file_ops = {}
                other = []
                for s in summaries:
                    normalized = self.task_builder._normalize_summary(s)
                    is_file_op = 'Read file:' in s or 'Edit file:' in s
                    if is_file_op:
                        if normalized not in file_ops:
                            file_ops[normalized] = s
                    else:
                        other.append(s)
                filtered = list(file_ops.values()) + other
            
            else:
                filtered = summaries[:len(summaries)//2] + summaries[-len(summaries)//4:]
            
            dedup_level += 1
        
        return filtered
    
    def format_task_text(self, task: Dict) -> str:
        """Use formatted_text from task builder directly."""
        if 'formatted_text' in task:
            return task['formatted_text']
        raise ValueError("Task must have 'formatted_text' from task builder")
    
    def _format_with_dedup(self, user_content: str, agent_summaries: List[str]) -> str:
        """Format task text with deduplicated summaries."""
        parts = [f"User: {user_content}"]
        for summary in agent_summaries:
            parts.append(f"Agent: {summary}")
        return "\n".join(parts)
    
    def get_message_tasks(self) -> List[Dict]:
        return self.task_builder.get_message_tasks()
    
    def get_embedding(self, text: str) -> Optional[np.ndarray]:
        max_retries = int(os.getenv('EMB_MAX_RETRIES', '3'))
        base_delay = float(os.getenv('EMB_RETRY_DELAY', '1.0'))
        
        for attempt in range(max_retries):
            try:
                self._last_embedding_error = None
                response = self.client.embeddings.create(
                    model=self.emb_model,
                    input=text
                )
                
                if response.data and len(response.data) > 0:
                    embedding = response.data[0].embedding
                    if embedding:
                        arr = np.array(embedding, dtype=np.float32)
                        if arr.ndim == 0 or len(arr) == 0:
                            return None
                        return arr
                return None
            except Exception as e:
                self._last_embedding_error = str(e)
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    if os.getenv('DEBUG_LLM') == '1':
                        print(f"    Embedding API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                        sys.stdout.flush()
                    time.sleep(delay)
                else:
                    return None
        return None
    
    def compress_embedding(self, embedding: np.ndarray) -> str:
        embedding_bytes = embedding.tobytes()
        compressed = gzip.compress(embedding_bytes)
        encoded = base64.b64encode(compressed).decode('ascii')
        return encoded
    
    def decompress_embedding(self, data: str) -> np.ndarray:
        compressed = base64.b64decode(data.encode('ascii'))
        decompressed = gzip.decompress(compressed)
        return np.frombuffer(decompressed, dtype=np.float32)
    
    def store_embedding(self, user_msg_id: int, embedding: np.ndarray, message_count: int, formatted_length: int = 0):
        encoded = self.compress_embedding(embedding)
        self.chats_cursor.execute("""
            INSERT OR REPLACE INTO task_embeddings (user_msg_id, embedding_data, message_count, formatted_length)
            VALUES (?, ?, ?, ?)
        """, (user_msg_id, encoded, message_count, formatted_length))
        self.chats_conn.commit()
    
    def extract_and_store_embeddings(self, tasks: List[Dict]):
        import sys
        total = len(tasks)
        print(f"Extracting embeddings for {total} tasks...")
        sys.stdout.flush()
        
        existing_count = 0
        processed_count = 0
        success_count = 0
        error_count = 0
        updated_count = 0
        
        # Create a lookup for tasks by user_msg_id
        task_lookup = {task['user_msg_id']: task for task in tasks}
        
        for i, task in enumerate(tasks):
            existing = self.chats_cursor.execute("""
                SELECT user_msg_id, formatted_length FROM task_embeddings WHERE user_msg_id = ?
            """, (task['user_msg_id'],)).fetchone()
            
            if existing:
                existing_count += 1
                # Update formatted_length if missing or different
                existing_length = existing[1] if existing[1] is not None else 0
                current_length = task.get('formatted_length', len(task['formatted_text']))
                
                if existing_length != current_length:
                    self.chats_cursor.execute("""
                        UPDATE task_embeddings 
                        SET formatted_length = ? 
                        WHERE user_msg_id = ?
                    """, (current_length, task['user_msg_id']))
                    self.chats_conn.commit()
                    updated_count += 1
                
                if (i + 1) % 100 == 0:
                    print(f"  Progress: {i+1}/{total} tasks (skipped: {existing_count}, updated: {updated_count}, processed: {processed_count}, success: {success_count}, errors: {error_count})")
                    sys.stdout.flush()
                continue
            
            if (i + 1) % 100 == 0 or i == 0:
                print(f"  Progress: {i+1}/{total} tasks (skipped: {existing_count}, processed: {processed_count}, success: {success_count}, errors: {error_count})")
                sys.stdout.flush()
            
            text = self.format_task_text(task)
            text_length = len(text)
            
            if text_length > self.context_size_chars:
                print(f"    Task {task['user_msg_id']}: Text length ({text_length:,} chars) exceeds context limit ({self.context_size_chars:,} chars), applying deduplication...")
                sys.stdout.flush()
                max_dedup_summaries = self._aggressive_deduplicate_summaries(task['agent_summaries'])
                text = self._format_with_dedup(task['user_content'], max_dedup_summaries)
                text_length = len(text)
                if text_length > self.context_size_chars:
                    print(f"    Task {task['user_msg_id']}: Still exceeds limit after deduplication ({text_length:,} chars), attempting anyway...")
                    sys.stdout.flush()
            
            embedding = self.get_embedding(text)
            processed_count += 1
            
            if embedding is not None:
                formatted_length = len(text)
                self.store_embedding(task['user_msg_id'], embedding, task['message_count'], formatted_length)
                success_count += 1
            else:
                error_msg = str(getattr(self, '_last_embedding_error', ''))
                if 'context_length_exceeded' in error_msg.lower() or 'too many tokens' in error_msg.lower() or 'context length' in error_msg.lower():
                    print(f"    Task {task['user_msg_id']}: Context overflow detected, applying maximum deduplication...")
                    sys.stdout.flush()
                    max_dedup_summaries = self._aggressive_deduplicate_summaries(task['agent_summaries'])
                    text_max_dedup = self._format_with_dedup(task['user_content'], max_dedup_summaries)
                    embedding = self.get_embedding(text_max_dedup)
                    if embedding is not None:
                        formatted_length = len(text_max_dedup)
                        self.store_embedding(task['user_msg_id'], embedding, task['message_count'], formatted_length)
                        success_count += 1
                        print(f"    Task {task['user_msg_id']}: Successfully embedded with maximum deduplication")
                        sys.stdout.flush()
                    else:
                        print(f"    Task {task['user_msg_id']}: Failed even with maximum deduplication")
                        sys.stdout.flush()
                        error_count += 1
                else:
                    error_count += 1
        
        print(f"Embedding extraction complete: {existing_count} already cached ({updated_count} updated with length), {success_count} new embeddings stored, {error_count} errors\n")
        sys.stdout.flush()
    
    def cosine_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        dot_product = np.dot(emb1, emb2)
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot_product / (norm1 * norm2)
    
    def run(self):
        import sys
        sys.stdout.flush()
        print("Extracting message tasks...")
        sys.stdout.flush()
        tasks = self.get_message_tasks()
        print(f"Found {len(tasks)} user message tasks\n")
        sys.stdout.flush()
        
        self.extract_and_store_embeddings(tasks)
    
    def close(self):
        self.chats_conn.close()


def main():
    load_dotenv()
    
    emb_url = os.getenv('EMB_URL')
    emb_model = os.getenv('EMB_MODEL')
    emb_api_key = os.getenv('EMB_API_KEY')
    
    if not emb_url or not emb_model:
        raise ValueError("EMB_URL and EMB_MODEL must be set in environment or .env file")
    
    parser = argparse.ArgumentParser(description='Extract embeddings for tasks')
    parser.add_argument('--db-file', default=None, help='Path to database file (default: searches for *.db files, uses most recent)')
    
    args = parser.parse_args()
    
    chats_db = args.db_file
    if chats_db is None:
        import glob
        db_files = glob.glob('*.db')
        if not db_files:
            raise ValueError("No database files found. Please specify --db-file or create a database with parse_chats.py")
        chats_db = max(db_files, key=os.path.getmtime)
        print(f"Using most recent database: {chats_db}")
    
    embedder = TaskEmbedder(chats_db, emb_url, emb_model, emb_api_key)
    try:
        embedder.run()
    finally:
        embedder.close()


if __name__ == '__main__':
    main()

