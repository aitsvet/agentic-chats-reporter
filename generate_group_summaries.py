#!/usr/bin/env python3
import sqlite3
import argparse
import sys
import os
from typing import List, Dict, Tuple
from datetime import datetime
from dotenv import load_dotenv
from task_builder import TaskBuilder
from llm_utils import tokens_to_chars, chars_to_tokens, get_effective_context_size, create_openai_client, load_api_config, DEFAULT_SUMMARY_PARAMS, get_llm_params, retry_with_backoff, clean_llm_response, get_llm_context_limit_and_max_tokens
from db_utils import find_db_file, add_db_file_argument
from common_utils import ProgressReporter


SUMMARY_SYSTEM_PROMPT = """Generate a task group summary covering ALL user tasks. Tasks are ordered chronologically.

Output format:
- First line: Title (5-7 words) - main theme covering all user tasks
- Blank line
- User task section (up to {USER_SUMMARY_MAX_TOKENS} tokens, IMPERATIVE VOICE): Summarize what user asked for - do NOT copy user messages verbatim. Include ALL user requirements from ALL tasks in a concise summary. Focus on what user asked for, NOT what agent did. ONE paragraph summarizing all user requests.
- Blank line
- Agent actions section (up to {AGENT_SUMMARY_MAX_TOKENS} tokens, PAST TENSE): Summarize actions across ALL tasks in a single unified summary. List actions chronologically: early actions first, refinements at end. Mention tools, files, commands. ONE paragraph summarizing all agent actions.

No emoji, checkmarks, or other formatting.
No section markers or labels - just the content.
Don't imagine or fake anything - only summarize what is in input task group content.

{PROMPT_EXTRA}"""


class GroupSummarizer:
    def __init__(self, chats_db: str, llm_url: str, llm_model: str, llm_api_key: str = None):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        
        llm_timeout = float(os.getenv('LLM_TIMEOUT', '300.0'))
        self.llm_client = create_openai_client(llm_url, llm_api_key, 'LLM_API_KEY', timeout=llm_timeout)
        self.llm_model = llm_model
        self.summary_params = get_llm_params('SUMMARY_PARAMS', DEFAULT_SUMMARY_PARAMS)
        
        self.user_summary_max_tokens = int(os.getenv('USER_SUMMARY_MAX_TOKENS', '320'))
        self.agent_summary_max_tokens = int(os.getenv('AGENT_SUMMARY_MAX_TOKENS', '480'))
        title_tokens = 20
        blank_lines_tokens = 5
        self.desired_output_tokens = self.user_summary_max_tokens + self.agent_summary_max_tokens + title_tokens + blank_lines_tokens
        
        self.overall_context_limit_tokens, self.api_max_tokens_param = get_llm_context_limit_and_max_tokens(
            self.llm_client, self.llm_model, self.summary_params
        )
        effective_size = get_effective_context_size(self.overall_context_limit_tokens, max_tokens=None)
        self.input_context_size_tokens = int(effective_size * 0.8)
        
        self.input_context_size_chars = tokens_to_chars(self.input_context_size_tokens)
        self.max_group_size_chars = self.input_context_size_chars
        self.task_builder = TaskBuilder(chats_db)
        self._create_tables()
        
        summary_prompt_template = os.getenv('SUMMARY_SYSTEM_PROMPT', SUMMARY_SYSTEM_PROMPT)
        prompt_extra = os.getenv('PROMPT_EXTRA', '').strip()
        self.summary_prompt_template = summary_prompt_template.format(
            USER_SUMMARY_MAX_TOKENS=self.user_summary_max_tokens,
            AGENT_SUMMARY_MAX_TOKENS=self.agent_summary_max_tokens,
            PROMPT_EXTRA=prompt_extra
        )
    
    
    def _create_tables(self):
        self.chats_cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_summaries (
                group_id INTEGER PRIMARY KEY,
                title TEXT,
                summary TEXT,
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
        """Format all tasks in a group for the prompt. Preserves chronological order (by date and start_line).
        Applies deduplication if needed to match what embeddings used."""
        user_msg_ids = [task['user_msg_id'] for task in tasks]
        
        stored_lengths = {}
        if user_msg_ids:
            placeholders = ','.join('?' * len(user_msg_ids))
            stored_data = self.chats_cursor.execute(f"""
                SELECT user_msg_id, formatted_length 
                FROM task_embeddings 
                WHERE user_msg_id IN ({placeholders})
            """, user_msg_ids).fetchall()
            stored_lengths = {user_msg_id: length for user_msg_id, length in stored_data}
        
        parts = []
        for i, task in enumerate(tasks, 1):
            user_msg_id = task['user_msg_id']
            original_text = task.get('formatted_text', '')
            original_length = len(original_text)
            stored_length = stored_lengths.get(user_msg_id, original_length)
            
            if stored_length < original_length * 0.9:
                deduped_summaries = self.task_builder.aggressive_deduplicate_summaries(task['agent_summaries'])
                task_text = self.task_builder.format_task_text(task['user_content'], deduped_summaries)
            else:
                task_text = original_text
            
            parts.append(task_text)
            if i < len(tasks):
                parts.append("")
        
        result = "\n".join(parts)
        result = result.replace('\n\n\n', '\n\n')
        while '\n\n\n' in result:
            result = result.replace('\n\n\n', '\n\n')
        
        result_length = len(result)
        prompt_overhead = len(self.summary_prompt_template) + 1000
        max_content_size = self.max_group_size_chars - prompt_overhead
        
        if result_length > max_content_size:
            print(f"    Warning: Group content ({result_length:,} chars) exceeds limit ({max_content_size:,} chars), applying additional deduplication...")
            sys.stdout.flush()
            
            deduped_parts = []
            for i, task in enumerate(tasks, 1):
                deduped_summaries = self.task_builder.aggressive_deduplicate_summaries(task['agent_summaries'])
                task_text = self.task_builder.format_task_text(task['user_content'], deduped_summaries)
                deduped_parts.append(task_text)
                if i < len(tasks):
                    deduped_parts.append("")
            
            result = "\n".join(deduped_parts)
            result = result.replace('\n\n\n', '\n\n')
            while '\n\n\n' in result:
                result = result.replace('\n\n\n', '\n\n')
            
            new_length = len(result)
            if new_length > max_content_size:
                print(f"    Warning: Still exceeds limit after deduplication ({new_length:,} chars), proceeding anyway...")
                sys.stdout.flush()
        
        return result
    
    def _extract_title(self, response_text: str) -> str:
        """Extract title from first line of LLM response."""
        response_text = response_text.strip()
        if not response_text:
            return "Untitled"
        first_line = response_text.split('\n')[0].strip()
        return first_line if first_line else "Untitled"
    
    def _extract_summary_content(self, response_text: str) -> str:
        """Extract summary content without the title line."""
        response_text = response_text.strip()
        if not response_text:
            return ""
        lines = response_text.split('\n')
        if len(lines) <= 1:
            return ""
        content = '\n'.join(lines[1:]).strip()
        return content
    
    @retry_with_backoff(retry_env_prefix='LLM', max_retries=10)
    def _call_llm_api(self, system_prompt: str, user_prompt: str) -> str:
        """Internal method to call LLM API with retry logic.
        
        max_tokens in API call is the OVERALL limit (input + output tokens).
        """
        if os.getenv('DEBUG_LLM') == '1':
            print(f"\n{'='*80}")
            print(f"LLM Request:")
            print(f"{'='*80}")
            print(f"\n[SYSTEM PROMPT]\n{system_prompt}")
            print(f"\n{'='*80}")
            print(f"[USER CONTENT] ({len(user_prompt)} characters)\n{user_prompt}")
            sys.stdout.flush()
        
        input_tokens = chars_to_tokens(len(system_prompt) + len(user_prompt))
        total_tokens_needed = input_tokens + self.desired_output_tokens
        
        if input_tokens > self.overall_context_limit_tokens:
            raise RuntimeError(f"Input tokens ({input_tokens}) exceed overall context limit ({self.overall_context_limit_tokens})")
        
        api_params = {
            'model': self.llm_model,
            'messages': [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        
        if self.api_max_tokens_param is not None:
            api_max_tokens = min(total_tokens_needed, self.api_max_tokens_param)
            if api_max_tokens > 0:
                api_params['max_tokens'] = api_max_tokens
        
        for key, value in self.summary_params.items():
            if value is not None and key != 'max_tokens':
                api_params[key] = value
        
        response = self.llm_client.chat.completions.create(**api_params)
        
        if not response.choices:
            error_details = f"Response has no choices. Response object: {type(response).__name__}"
            if hasattr(response, 'model'):
                error_details += f", model: {response.model}"
            if hasattr(response, 'usage'):
                error_details += f", usage: {response.usage}"
            raise RuntimeError(f"LLM response has no choices: {error_details}")
        
        choice = response.choices[0]
        finish_reason = getattr(choice, 'finish_reason', 'unknown')
        
        if not hasattr(choice, 'message') or not choice.message:
            error_details = f"Response choice has no message. Choice: {choice}, finish_reason: {finish_reason}"
            raise RuntimeError(f"LLM response has no message: {error_details}")
        
        if not choice.message.content:
            error_details = f"Response message has no content. Finish reason: {finish_reason}"
            if finish_reason == 'content_filter':
                error_details += " (content was filtered by safety system)"
            elif finish_reason == 'length':
                error_details += " (response was truncated due to length limit)"
            elif finish_reason == 'stop':
                error_details += " (model stopped but produced no content - possible refusal)"
            if hasattr(choice.message, 'refusal'):
                error_details += f", refusal: {choice.message.refusal}"
            if hasattr(response, 'model'):
                error_details += f", model: {response.model}"
            raise RuntimeError(f"LLM response has no content: {error_details}")
        
        response_text = choice.message.content.strip()
        response_text = clean_llm_response(response_text)
        
        if os.getenv('DEBUG_LLM') == '1':
            print(f"{'='*80}\n")
            print(f"\n{'='*80}")
            print(f"LLM Response:")
            print(f"{'='*80}")
            print(f"\n{response_text}")
            print(f"{'='*80}\n")
            sys.stdout.flush()
        
        if not response_text:
            raise RuntimeError("LLM response is empty after stripping")
        
        return response_text
    
    def generate_summary(self, content: str) -> Tuple[str, str]:
        """Call LLM to generate summary. Returns (title, summary).
        
        Raises RuntimeError if LLM API call fails after all retries.
        Raises ValueError if response is empty.
        """
        system_prompt = self.summary_prompt_template
        user_prompt = content
        
        response_text = self._call_llm_api(system_prompt, user_prompt)
        title = self._extract_title(response_text)
        summary_content = self._extract_summary_content(response_text)
        return title, summary_content
    
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
        3. The summary fields (title, summary) are non-empty
        """
        stored_summary = self.chats_cursor.execute("""
            SELECT task_count, title, summary FROM group_summaries WHERE group_id = ?
        """, (group_id,)).fetchone()
        
        if not stored_summary:
            return False
        
        stored_task_count, title, summary = stored_summary
        
        if not title or not title.strip():
            return False
        
        if not summary or not summary.strip():
            return False
        
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
            SELECT group_id, title, summary, first_timestamp, task_count
            FROM group_summaries WHERE group_id = ?
        """, (group_id,)).fetchone()
        
        if row:
            title = row[1]
            summary = row[2] or ""
            if summary:
                summary_lines = summary.strip().split('\n')
                if summary_lines and summary_lines[0].strip() == title:
                    summary = '\n'.join(summary_lines[1:]).strip()
            
            return {
                'group_id': row[0],
                'title': title,
                'summary': summary,
                'first_timestamp': datetime.strptime(row[3], '%Y-%m-%d %H:%MZ') if row[3] else datetime.min,
                'task_count': row[4]
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
        
        progress = ProgressReporter(total=len(groups), interval=10)
        
        for idx, (group_id, first_timestamp) in enumerate(groups, 1):
            progress.update(skipped=skipped_count)
            
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
                                if existing['summary']:
                                    f.write(f"{existing['summary']}\n\n")
                                f.write("---\n\n")
                                f.flush()
                        continue
                else:
                    print(f"  Group {group_id} has invalid summary, will regenerate...")
                    sys.stdout.flush()
            
            tasks = self.get_group_tasks(group_id)
            if not tasks:
                print(f"  Group {group_id} has no tasks, skipping...")
                sys.stdout.flush()
                continue
            
            content = self.format_group_content(tasks)
            content_size = len(content)
            print(f"  Processing group {group_id} ({len(tasks)} tasks, {content_size:,} chars)...")
            sys.stdout.flush()
            
            try:
                title, summary = self.generate_summary(content)
            except (RuntimeError, ValueError) as e:
                error_msg = f"\nError generating summary for group {group_id}: {e}"
                error_msg += f"\n  Group details: {len(tasks)} tasks, {content_size:,} chars"
                error_msg += f"\n  Input tokens (approx): {chars_to_tokens(content_size + len(self.summary_prompt_template))}"
                error_msg += f"\n  Context limit: {self.overall_context_limit_tokens} tokens"
                print(error_msg, file=sys.stderr)
                sys.stderr.flush()
                raise
            
            result = {
                'group_id': group_id,
                'first_timestamp': first_timestamp,
                'task_count': len(tasks),
                'title': title,
                'summary': summary
            }
            results.append(result)
            
            self.chats_cursor.execute("""
                INSERT OR REPLACE INTO group_summaries 
                (group_id, title, summary, first_timestamp, task_count)
                VALUES (?, ?, ?, ?, ?)
            """, (
                group_id,
                title,
                summary,
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
                    if summary:
                        f.write(f"{summary}\n\n")
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
                if result.get('summary'):
                    f.write(f"{result['summary']}\n\n")
                f.write("---\n\n")
        
        print(f"\nReport written to: {output_file}")
    
    def close(self):
        self.chats_conn.close()


def main():
    load_dotenv()
    
    config = load_api_config(['llm_url', 'llm_model'])
    
    parser = argparse.ArgumentParser(description='Generate summaries for task groups using LLM')
    add_db_file_argument(parser)
    parser.add_argument('--output', default='group_summaries.md', help='Output markdown file (default: group_summaries.md)')
    parser.add_argument('--force', action='store_true', help='Force re-generation of all summaries even if they exist')
    
    args = parser.parse_args()
    
    chats_db = find_db_file(args.db_file)
    
    summarizer = GroupSummarizer(chats_db, config['llm_url'], config['llm_model'], config['llm_api_key'])
    try:
        results = summarizer.generate_all_summaries(output_file=args.output, skip_if_exists=not args.force)
        if not results:
            print("No results generated, but file may have been written incrementally")
    finally:
        summarizer.close()


if __name__ == '__main__':
    main()

