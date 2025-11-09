#!/usr/bin/env python3
import sqlite3
import argparse
import sys
import os
import time
from typing import List, Dict, Tuple
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from task_builder import TaskBuilder


SUMMARY_SYSTEM_PROMPT = """Generate a title and two summaries covering ALL user tasks. Tasks are ordered chronologically.

**Title** (5-7 words): Main theme covering all user tasks.

**User Summary** (up to {USER_REPORT_MAX_LINES} lines, IMPERATIVE VOICE):
- Include ALL user requirements from ALL tasks
- Focus on what user asked for, NOT what agent did
- First half tasks at BEGINNING, second half/end at END
- Mention decision changes at end with context ("then changed to...", "later modified...")
- Establish original request before describing changes

**Agent Summary** (up to {AGENT_REPORT_MAX_LINES} lines, PAST TENSE):
- List actions across ALL tasks
- Mention tools, files, commands
- Chronological: early actions first, refinements at end

**Output Format (use exactly this structure, ready for report insertion):**
[Title text - 5-7 words on first line]

**User:**
[Summary text in imperative voice]

**Agent:**
[Summary text in past tense]

Output ONLY the above format. No markdown lists, checkmarks, or other formatting. Title on first line, then blank line, then User section, then Agent section.

{PROMPT_EXTRA}"""


class GroupSummarizer:
    def __init__(self, chats_db: str, llm_url: str, llm_model: str, llm_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        api_key = llm_api_key or os.getenv('LLM_API_KEY', 'not-needed')
        self.llm_client = OpenAI(base_url=llm_url, api_key=api_key)
        self.llm_model = llm_model
        self.task_builder = TaskBuilder(chats_db)
        self._create_tables()
        
        summary_prompt_template = os.getenv('SUMMARY_SYSTEM_PROMPT', SUMMARY_SYSTEM_PROMPT)
        prompt_extra = os.getenv('PROMPT_EXTRA', '').strip()
        self.summary_prompt_template = summary_prompt_template.format(
            USER_REPORT_MAX_LINES=int(os.getenv('USER_REPORT_MAX_LINES', '15')),
            AGENT_REPORT_MAX_LINES=int(os.getenv('AGENT_REPORT_MAX_LINES', '10')),
            PROMPT_EXTRA=prompt_extra
        )
        self.summary_temperature = float(os.getenv('SUMMARY_TEMPERATURE', '0.3'))
    
    def _create_tables(self):
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_summaries (
                group_id INTEGER PRIMARY KEY,
                title TEXT,
                user_summary TEXT,
                agent_summary TEXT,
                first_timestamp TEXT,
                task_count INTEGER
            )
        """)
        self.chats_conn.commit()
    
    def get_group_tasks(self, group_id: int) -> List[Dict]:
        """Get all tasks in a group, ordered by user message timestamp and source start line number."""
        tasks = self.chats_cursor.execute("""
            SELECT sg.user_msg_id, m.message_datetime, m.chat_id
            FROM task_groups sg
            JOIN messages m ON sg.user_msg_id = m.id
            WHERE sg.threshold = -1.0 AND sg.group_id = ?
            ORDER BY m.message_datetime ASC, m.start_line ASC
        """, (group_id,)).fetchall()
        
        result = []
        for user_msg_id, msg_datetime, chat_id in tasks:
            task = self.task_builder.get_task_for_user_message(user_msg_id)
            if task:
                task['message_datetime'] = msg_datetime
                task['chat_id'] = chat_id
                result.append(task)
        
        return result
    
    def format_group_content(self, tasks: List[Dict]) -> str:
        """Format all tasks in a group for the prompt. Preserves chronological order (by date and start_line)."""
        parts = []
        for i, task in enumerate(tasks, 1):
            parts.append(task.get('formatted_text', ''))
            if i < len(tasks):
                parts.append("")
        
        result = "\n".join(parts)
        result = result.replace('\n\n\n', '\n\n')
        while '\n\n\n' in result:
            result = result.replace('\n\n\n', '\n\n')
        return result
    
    def _parse_llm_response(self, response_text: str) -> Tuple[str, str, str]:
        """Parse LLM response into title, user_summary, and agent_summary.
        
        Raises ValueError if response cannot be parsed (empty or missing required sections).
        """
        lines = response_text.strip().split('\n')
        
        if not lines:
            raise ValueError("LLM response is empty")
        
        title = lines[0].strip()
        
        if not title:
            raise ValueError("LLM response has no title on first line")
        
        user_section_start = None
        agent_section_start = None
        
        for i, line in enumerate(lines):
            if line.strip().startswith('**User:**') or line.strip().startswith('User:'):
                user_section_start = i + 1
            elif line.strip().startswith('**Agent:**') or line.strip().startswith('Agent:'):
                agent_section_start = i + 1
                if user_section_start is not None:
                    user_summary = '\n'.join(lines[user_section_start:agent_section_start-1]).strip()
                break
        
        if user_section_start is None:
            raise ValueError("LLM response missing User section (expected '**User:**' or 'User:')")
        
        if agent_section_start is None:
            raise ValueError("LLM response missing Agent section (expected '**Agent:**' or 'Agent:')")
        
        user_summary = '\n'.join(lines[user_section_start:agent_section_start-1]).strip()
        agent_summary = '\n'.join(lines[agent_section_start:]).strip()
        
        return title, user_summary, agent_summary
    
    def generate_summary(self, content: str) -> Tuple[str, str, str]:
        """Call LLM to generate summary. Returns (title, user_summary, agent_summary).
        
        Raises RuntimeError if LLM API call fails after all retries.
        Raises ValueError if response is empty or cannot be parsed.
        """
        max_retries = int(os.getenv('LLM_MAX_RETRIES', '3'))
        base_delay = float(os.getenv('LLM_RETRY_DELAY', '1.0'))
        
        system_prompt = self.summary_prompt_template
        user_prompt = content
        
        last_error = None
        
        for attempt in range(max_retries):
            try:
                response = self.llm_client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=self.summary_temperature
                )
                
                if not response.choices or not response.choices[0].message.content:
                    raise RuntimeError("LLM response has no content")
                
                response_text = response.choices[0].message.content.strip()
                
                if os.getenv('DEBUG_LLM') == '1':
                    print(f"\n{'='*80}")
                    print(f"LLM Request:")
                    print(f"{'='*80}")
                    print(f"\n[SYSTEM PROMPT]\n{system_prompt}")
                    print(f"\n{'='*80}")
                    print(f"[USER CONTENT] ({len(user_prompt)} characters)\n{user_prompt}")
                    print(f"{'='*80}\n")
                    print(f"\n{'='*80}")
                    print(f"LLM Response:")
                    print(f"{'='*80}")
                    print(f"\n{response_text}")
                    print(f"{'='*80}\n")
                    sys.stdout.flush()
                
                if not response_text:
                    raise RuntimeError("LLM response is empty after stripping")
                
                try:
                    title, user_summary, agent_summary = self._parse_llm_response(response_text)
                    return title, user_summary, agent_summary
                except ValueError as parse_error:
                    raise RuntimeError(f"Failed to parse LLM response: {parse_error}") from parse_error
                
            except RuntimeError as e:
                if "Failed to parse LLM response" in str(e):
                    raise
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    if os.getenv('DEBUG_LLM') == '1':
                        print(f"    LLM API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                        sys.stdout.flush()
                    time.sleep(delay)
                else:
                    raise RuntimeError(f"Failed to generate summary after {max_retries} attempts: {e}") from e
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    if os.getenv('DEBUG_LLM') == '1':
                        print(f"    LLM API error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {delay:.1f}s...")
                        sys.stdout.flush()
                    time.sleep(delay)
                else:
                    raise RuntimeError(f"Failed to generate summary after {max_retries} attempts: {e}") from e
        
        raise RuntimeError(f"Failed to generate summary after {max_retries} attempts: {last_error}") from last_error
    
    def get_all_groups(self) -> List[Tuple[int, datetime]]:
        """Get all groups with their first message timestamp."""
        groups = self.chats_cursor.execute("""
            SELECT DISTINCT sg.group_id, MIN(m.message_datetime) as first_timestamp, MIN(m.start_line) as first_start_line
            FROM task_groups sg
            JOIN messages m ON sg.user_msg_id = m.id
            WHERE sg.threshold = -1.0
            GROUP BY sg.group_id
            ORDER BY first_timestamp ASC, first_start_line ASC
        """).fetchall()
        
        result = []
        for gid, ts, _ in groups:
            if ts:
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%MZ")
                    result.append((gid, dt))
                except:
                    result.append((gid, datetime.min))
            else:
                result.append((gid, datetime.min))
        
        return result
    
    def has_summary(self, group_id: int) -> bool:
        """Check if summary already exists for a group."""
        count = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM group_summaries WHERE group_id = ?
        """, (group_id,)).fetchone()[0]
        return count > 0
    
    def validate_group_summary(self, group_id: int) -> bool:
        """
        Validate that a group's summary is still valid by checking:
        1. All tasks in the group still exist
        2. The group's task count matches the stored task_count
        """
        stored_summary = self.chats_cursor.execute("""
            SELECT task_count FROM group_summaries WHERE group_id = ?
        """, (group_id,)).fetchone()
        
        if not stored_summary:
            return False
        
        stored_task_count = stored_summary[0]
        
        current_tasks = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM task_groups 
            WHERE group_id = ? AND threshold = -1.0
        """, (group_id,)).fetchone()[0]
        
        if current_tasks != stored_task_count:
            return False
        
        all_tasks_exist = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM task_groups tg
            JOIN messages m ON tg.user_msg_id = m.id
            WHERE tg.group_id = ? AND tg.threshold = -1.0
        """, (group_id,)).fetchone()[0]
        
        return all_tasks_exist == stored_task_count
    
    def get_existing_summary(self, group_id: int) -> Dict:
        """Get existing summary for a group."""
        row = self.chats_cursor.execute("""
            SELECT group_id, title, user_summary, agent_summary, first_timestamp, task_count
            FROM group_summaries WHERE group_id = ?
        """, (group_id,)).fetchone()
        
        if row:
            return {
                'group_id': row[0],
                'title': row[1],
                'user_summary': row[2],
                'agent_summary': row[3],
                'first_timestamp': datetime.strptime(row[4], '%Y-%m-%d %H:%MZ') if row[4] else datetime.min,
                'task_count': row[5]
            }
        return None
    
    def generate_all_summaries(self, output_file: str = None, skip_if_exists: bool = True) -> List[Dict]:
        """Generate summaries for all groups, optionally writing to file incrementally."""
        groups = self.get_all_groups()
        print(f"Found {len(groups)} groups to summarize")
        
        if skip_if_exists:
            existing_count = sum(1 for gid, _ in groups if self.has_summary(gid))
            if existing_count > 0:
                print(f"  {existing_count} summaries already exist, will skip those")
        print()
        
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write("# Task Summaries\n\n")
                f.write("---\n\n")
                f.flush()
        
        results = []
        toc_position = 0
        skipped_count = 0
        
        for idx, (group_id, first_timestamp) in enumerate(groups, 1):
            if idx % 10 == 0 or idx == 1:
                print(f"  Progress: {idx}/{len(groups)} groups (skipped: {skipped_count})")
                sys.stdout.flush()
            
            if skip_if_exists and self.has_summary(group_id):
                if self.validate_group_summary(group_id):
                    existing = self.get_existing_summary(group_id)
                    if existing:
                        results.append(existing)
                        skipped_count += 1
                        timestamp_str = existing['first_timestamp'].strftime('%Y-%m-%d %H:%MZ')
                        toc_entry = f"{idx}. {existing['title']} ({timestamp_str})"
                        
                        if output_file:
                            with open(output_file, 'r+', encoding='utf-8') as f:
                                content_lines = f.readlines()
                                toc_end_idx = next((i for i, line in enumerate(content_lines) if line.strip() == '---'), len(content_lines))
                                content_lines.insert(toc_end_idx, f"{toc_entry}\n")
                                f.seek(0)
                                f.writelines(content_lines)
                                f.truncate()
                                f.flush()
                            
                            with open(output_file, 'a', encoding='utf-8') as f:
                                f.write(f"## {idx}. {existing['title']} ({timestamp_str})\n\n")
                                if existing['user_summary']:
                                    f.write(f"**User:**\n{existing['user_summary']}\n\n")
                                if existing['agent_summary']:
                                    f.write(f"**Agent:**\n{existing['agent_summary']}\n\n")
                                f.write("---\n\n")
                                f.flush()
                        continue
            
            tasks = self.get_group_tasks(group_id)
            if not tasks:
                continue
            
            content = self.format_group_content(tasks)
            try:
                title, user_summary, agent_summary = self.generate_summary(content)
            except (RuntimeError, ValueError) as e:
                print(f"\nError generating summary for group {group_id}: {e}", file=sys.stderr)
                raise
            
            result = {
                'group_id': group_id,
                'first_timestamp': first_timestamp,
                'task_count': len(tasks),
                'title': title,
                'user_summary': user_summary,
                'agent_summary': agent_summary
            }
            results.append(result)
            
            self.chats_cursor.execute("""
                INSERT OR REPLACE INTO group_summaries 
                (group_id, title, user_summary, agent_summary, first_timestamp, task_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                group_id,
                title,
                user_summary,
                agent_summary,
                first_timestamp.strftime('%Y-%m-%d %H:%MZ'),
                len(tasks)
            ))
            self.chats_conn.commit()
            
            timestamp_str = first_timestamp.strftime('%Y-%m-%d %H:%MZ')
            toc_entry = f"{idx}. {title} ({timestamp_str})"
            
            if output_file:
                with open(output_file, 'r+', encoding='utf-8') as f:
                    content_lines = f.readlines()
                    toc_end_idx = next((i for i, line in enumerate(content_lines) if line.strip() == '---'), len(content_lines))
                    content_lines.insert(toc_end_idx, f"{toc_entry}\n")
                    f.seek(0)
                    f.writelines(content_lines)
                    f.truncate()
                    f.flush()
                
                with open(output_file, 'a', encoding='utf-8') as f:
                    f.write(f"## {idx}. {title} ({timestamp_str})\n\n")
                    if user_summary:
                        f.write(f"**User:**\n{user_summary}\n\n")
                    if agent_summary:
                        f.write(f"**Agent:**\n{agent_summary}\n\n")
                    f.write("---\n\n")
                    f.flush()
        
        if output_file:
            print(f"\nReport written incrementally to: {output_file}")
        
        return results
    
    def write_markdown_report(self, results: List[Dict], output_file: str):
        """Write all summaries to markdown file."""
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("# Task Summaries\n\n")
            f.write(f"Generated from {len(results)} groups\n\n")
            f.write("---\n\n")
            
            for idx, result in enumerate(results, 1):
                title = result.get('title', 'Group Summary').strip()
                timestamp_str = result['first_timestamp'].strftime('%Y-%m-%d %H:%MZ')
                f.write(f"# {idx}. {title} ({timestamp_str})\n\n")
                if result['user_summary']:
                    f.write(f"**User:**\n{result['user_summary']}\n\n")
                if result['agent_summary']:
                    f.write(f"**Agent:**\n{result['agent_summary']}\n\n")
                f.write("---\n\n")
        
        print(f"\nReport written to: {output_file}")
    
    def close(self):
        self.chats_conn.close()


def main():
    load_dotenv()
    
    llm_url = os.getenv('LLM_URL')
    llm_model = os.getenv('LLM_MODEL')
    llm_api_key = os.getenv('LLM_API_KEY')
    
    if not llm_url or not llm_model:
        raise ValueError("LLM_URL and LLM_MODEL must be set in environment or .env file")
    
    parser = argparse.ArgumentParser(description='Generate summaries for task groups using LLM')
    parser.add_argument('--db-file', default=None, help='Path to database file (default: searches for *.db files, uses most recent)')
    parser.add_argument('--output', default='group_summaries.md', help='Output markdown file (default: group_summaries.md)')
    parser.add_argument('--force', action='store_true', help='Force re-generation of all summaries even if they exist')
    
    args = parser.parse_args()
    
    chats_db = args.db_file
    if chats_db is None:
        import glob
        db_files = glob.glob('*.db')
        if not db_files:
            raise ValueError("No database files found. Please specify --db-file or create a database with parse_chats.py")
        chats_db = max(db_files, key=os.path.getmtime)
        print(f"Using most recent database: {chats_db}")
    
    summarizer = GroupSummarizer(chats_db, llm_url, llm_model, llm_api_key)
    try:
        results = summarizer.generate_all_summaries(output_file=args.output, skip_if_exists=not args.force)
        if not results:
            print("No results generated, but file may have been written incrementally")
    finally:
        summarizer.close()


if __name__ == '__main__':
    main()

