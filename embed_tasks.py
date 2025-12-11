#!/usr/bin/env python3
import sqlite3
import argparse
from typing import List, Dict, Optional
import numpy as np
from dotenv import load_dotenv
from task_builder import TaskBuilder
from llm_utils import get_model_context_size, tokens_to_chars, create_openai_client, load_api_config
from embedding_utils import compress_embedding as compress_embedding_util, decompress_embedding as decompress_embedding_util, cosine_similarity
from db_utils import find_db_file, add_db_file_argument, safe_add_column
from common_utils import ProgressReporter


class TaskEmbedder:
    def __init__(self, chats_db: str, emb_url: str, emb_model: str, emb_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        self.emb_model = emb_model
        self.client = create_openai_client(emb_url, emb_api_key, 'EMB_API_KEY')
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
        
        safe_add_column(self.chats_cursor, 'task_embeddings', 'formatted_length INTEGER')
        
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
        from llm_utils import retry_with_backoff
        
        @retry_with_backoff(retry_env_prefix='EMB')
        def _call_api():
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
        
        try:
            return _call_api()
        except RuntimeError:
            return None
    
    def compress_embedding(self, embedding: np.ndarray) -> str:
        return compress_embedding_util(embedding)
    
    def decompress_embedding(self, data: str) -> np.ndarray:
        return decompress_embedding_util(data)
    
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
        
        progress = ProgressReporter(total=total)
        
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
                
                progress.update(skipped=existing_count, updated=updated_count, processed=processed_count, success=success_count, errors=error_count)
                continue
            
            progress.update(skipped=existing_count, processed=processed_count, success=success_count, errors=error_count)
            
            text = self.format_task_text(task)
            text_length = len(text)
            
            if text_length > self.context_size_chars:
                print(f"    Task {task['user_msg_id']}: Text length ({text_length:,} chars) exceeds context limit ({self.context_size_chars:,} chars), applying deduplication...")
                sys.stdout.flush()
                max_dedup_summaries = self.task_builder.aggressive_deduplicate_summaries(task['agent_summaries'])
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
                print(f"    Task {task['user_msg_id']}: Context overflow detected, applying maximum deduplication...")
                sys.stdout.flush()
                max_dedup_summaries = self.task_builder.aggressive_deduplicate_summaries(task['agent_summaries'])
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
        
        print(f"Embedding extraction complete: {existing_count} already cached ({updated_count} updated with length), {success_count} new embeddings stored, {error_count} errors\n")
        sys.stdout.flush()
    
    def cosine_similarity(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        return cosine_similarity(emb1, emb2)
    
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
    
    config = load_api_config(['emb_url', 'emb_model'])
    
    parser = argparse.ArgumentParser(description='Extract embeddings for tasks')
    add_db_file_argument(parser)
    
    args = parser.parse_args()
    
    chats_db = find_db_file(args.db_file)
    
    embedder = TaskEmbedder(chats_db, config['emb_url'], config['emb_model'], config['emb_api_key'])
    try:
        embedder.run()
    finally:
        embedder.close()


if __name__ == '__main__':
    main()

