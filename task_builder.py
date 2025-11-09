#!/usr/bin/env python3
import sqlite3
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple


class TaskBuilder:
    def __init__(self, chats_db: str):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
    
    def parse_chat_datetime(self, dt_str: str) -> Optional[float]:
        if not dt_str:
            return None
        try:
            dt_naive = datetime.strptime(dt_str, "%Y-%m-%d %H:%MZ")
            if dt_str.endswith('Z'):
                dt_utc = dt_naive.replace(tzinfo=timezone.utc)
                return dt_utc.timestamp()
            else:
                return dt_naive.timestamp()
        except:
            return None
    
    def _get_tool_output_length(self, content_text: Optional[str], tool_type: Optional[str], tool_name: Optional[str]) -> int:
        if not content_text:
            return 0
        
        if tool_type == 'write' or (tool_name and tool_name in ['write', 'apply_patch', 'search_replace']):
            return len(content_text)
        else:
            summary_match = re.search(r'<summary>(.*?)</summary>', content_text, re.DOTALL)
            return len(summary_match.group(1)) if summary_match else 0
    
    def get_task_for_user_message(self, user_msg_id: int) -> Optional[Dict]:
        """Get a single task for a specific user message."""
        user_msg = self.chats_cursor.execute("""
            SELECT id, chat_id, message_datetime, content_length
            FROM messages
            WHERE id = ? AND message_type = 'User' AND message_datetime IS NOT NULL
        """, (user_msg_id,)).fetchone()
        
        if not user_msg:
            return None
        
        chat_id, user_dt_str, user_content_len = user_msg[1], user_msg[2], user_msg[3]
        user_timestamp = self.parse_chat_datetime(user_dt_str)
        if not user_timestamp:
            return None
        
        agent_messages = self.chats_cursor.execute("""
            SELECT id, message_datetime, content_length, content_type, summary, agent_summary
            FROM messages
            WHERE chat_id = ? 
            AND message_type = 'Agent'
            AND id > ?
            AND id < COALESCE(
                (SELECT MIN(id) FROM messages 
                 WHERE chat_id = ? AND message_type = 'User' AND id > ?),
                999999999
            )
            ORDER BY message_datetime, start_line
        """, (chat_id, user_msg_id, chat_id, user_msg_id)).fetchall()
        
        user_content = self.chats_cursor.execute("""
            SELECT content_text FROM content WHERE message_id = ?
        """, (user_msg_id,)).fetchone()
        
        user_content_text = user_content[0] if user_content else ""
        
        agent_summaries = []
        agent_timestamps = []
        last_agent_timestamp = user_timestamp
        
        for agent_msg_id, agent_dt_str, agent_content_len, content_type, summary, agent_summary in agent_messages:
            agent_timestamp = self.parse_chat_datetime(agent_dt_str) if agent_dt_str else None
            if agent_timestamp:
                agent_timestamps.append(agent_timestamp)
                last_agent_timestamp = max(last_agent_timestamp, agent_timestamp)
            
            if content_type == 'tool_call':
                if agent_summary:
                    agent_summaries.append(agent_summary)
                elif summary:
                    agent_summaries.append(summary)
                else:
                    content_result = self.chats_cursor.execute("""
                        SELECT content_text FROM content WHERE message_id = ?
                    """, (agent_msg_id,)).fetchone()
                    if content_result:
                        content_text = content_result[0]
                        summary_match = re.search(r'<summary>(.*?)</summary>', content_text, re.DOTALL)
                        if summary_match:
                            summary_text = summary_match.group(1).strip()
                            agent_summaries.append(summary_text)
                        elif agent_content_len:
                            agent_summaries.append(f"Tool call ({agent_content_len} chars)")
                    elif agent_content_len:
                        agent_summaries.append(f"Tool call ({agent_content_len} chars)")
            elif content_type == 'text':
                if agent_summary:
                    agent_summaries.append(agent_summary)
                elif summary:
                    agent_summaries.append(summary)
                else:
                    content_result = self.chats_cursor.execute("""
                        SELECT content_text FROM content WHERE message_id = ?
                    """, (agent_msg_id,)).fetchone()
                    if content_result and content_result[0]:
                        first_line = content_result[0].split('\n')[0]
                        agent_summaries.append(first_line)
                    elif agent_content_len:
                        agent_summaries.append(f"Agent message ({agent_content_len} chars)")
            elif agent_summary:
                agent_summaries.append(agent_summary)
            elif summary:
                agent_summaries.append(summary)
            elif agent_content_len:
                agent_summaries.append(f"Agent message ({agent_content_len} chars)")
        
        filtered_summaries = self._filter_summaries(agent_summaries)
        
        task_end_timestamp = last_agent_timestamp + 60
        formatted_text = self.format_task_text(user_content_text, filtered_summaries)
        
        return {
            'user_msg_id': user_msg_id,
            'chat_id': chat_id,
            'user_content': user_content_text,
            'agent_summaries': filtered_summaries,
            'message_count': 1 + len(agent_messages),
            'formatted_text': formatted_text,
            'formatted_length': len(formatted_text),
            'user_timestamp': user_timestamp,
            'task_end_timestamp': task_end_timestamp
        }
    
    def get_message_tasks(self) -> List[Dict]:
        tasks = []
        
        user_messages = self.chats_cursor.execute("""
            SELECT id, chat_id, message_datetime, content_length
            FROM messages
            WHERE message_type = 'User' AND message_datetime IS NOT NULL
            ORDER BY message_datetime, start_line
        """).fetchall()
        
        for user_msg_id, chat_id, user_dt_str, user_content_len in user_messages:
            user_timestamp = self.parse_chat_datetime(user_dt_str)
            if not user_timestamp:
                continue
            
            agent_messages = self.chats_cursor.execute("""
                SELECT id, message_datetime, content_length, content_type, summary
                FROM messages
                WHERE chat_id = ? 
                AND message_type = 'Agent'
                AND id > ?
                AND id < COALESCE(
                    (SELECT MIN(id) FROM messages 
                     WHERE chat_id = ? AND message_type = 'User' AND id > ?),
                    999999999
                )
                ORDER BY message_datetime, start_line
            """, (chat_id, user_msg_id, chat_id, user_msg_id)).fetchall()
            
            user_content = self.chats_cursor.execute("""
                SELECT content_text FROM content WHERE message_id = ?
            """, (user_msg_id,)).fetchone()
            
            user_content_text = user_content[0] if user_content else ""
            
            # Calculate usage correlation fields
            total_content_length = (user_content_len or 0)
            agent_text_length = 0
            agent_timestamps = []
            last_agent_timestamp = user_timestamp
            
            # Extract agent summaries for embedding
            agent_summaries = []
            
            for agent_msg_id, agent_dt_str, agent_content_len, agent_content_type, agent_summary in agent_messages:
                total_content_length += (agent_content_len or 0)
                
                # Handle timestamps for correlation
                if agent_dt_str:
                    agent_ts = self.parse_chat_datetime(agent_dt_str)
                    if agent_ts:
                        agent_timestamps.append(agent_ts)
                        if agent_ts > last_agent_timestamp:
                            last_agent_timestamp = agent_ts
                
                # Extract summaries for embedding
                if agent_content_type == 'tool_call':
                    # Use truncated summary from database if available, otherwise extract from content
                    if agent_summary:
                        agent_summaries.append(agent_summary)
                    else:
                        content_result = self.chats_cursor.execute("""
                            SELECT content_text FROM content WHERE message_id = ?
                        """, (agent_msg_id,)).fetchone()
                        if content_result:
                            content_text = content_result[0]
                            summary_match = re.search(r'<summary>(.*?)</summary>', content_text, re.DOTALL)
                            if summary_match:
                                summary_text = summary_match.group(1).strip()
                                agent_summaries.append(summary_text)
                            elif agent_content_len:
                                agent_summaries.append(f"Tool call ({agent_content_len} chars)")
                        elif agent_content_len:
                            agent_summaries.append(f"Tool call ({agent_content_len} chars)")
                    
                    # Calculate text length for correlation
                    tool_info = self.chats_cursor.execute("""
                        SELECT data_tool_type, data_tool_name, 
                               (SELECT content_text FROM content WHERE message_id = ?) as content_text
                        FROM messages WHERE id = ?
                    """, (agent_msg_id, agent_msg_id)).fetchone()
                    
                    if tool_info:
                        tool_type, tool_name, content_text = tool_info
                        output_len = self._get_tool_output_length(content_text, tool_type, tool_name)
                        agent_text_length += output_len
                elif agent_content_type == 'text':
                    # Use summary (first line) from database, or fallback to content
                    if agent_summary:
                        agent_summaries.append(agent_summary)
                    else:
                        content_result = self.chats_cursor.execute("""
                            SELECT content_text FROM content WHERE message_id = ?
                        """, (agent_msg_id,)).fetchone()
                        if content_result and content_result[0]:
                            first_line = content_result[0].split('\n')[0]
                            agent_summaries.append(first_line)
                        elif agent_content_len:
                            agent_summaries.append(f"Agent message ({agent_content_len} chars)")
                    
                    agent_text_length += (agent_content_len or 0)
                elif agent_summary:
                    agent_summaries.append(agent_summary)
                elif agent_content_len:
                    agent_summaries.append(f"Agent message ({agent_content_len} chars)")
            
            if not agent_timestamps:
                last_agent_timestamp = user_timestamp + 300
            
            # Filter out error messages and adjacent duplicates
            filtered_summaries = self._filter_summaries(agent_summaries)
            
            task_end_timestamp = last_agent_timestamp + 60
            formatted_text = self.format_task_text(user_content_text, filtered_summaries)
            
            tasks.append({
                'user_msg_id': user_msg_id,
                'chat_id': chat_id,
                'user_timestamp': user_timestamp,
                'user_datetime_str': user_dt_str,
                'user_content': user_content_text,
                'agent_summaries': filtered_summaries,
                'message_count': 1 + len(agent_messages),
                'formatted_text': formatted_text,
                'formatted_length': len(formatted_text),
                # Fields for correlation
                'task_end_timestamp': task_end_timestamp,
                'total_content_length': total_content_length,
                'user_content_length': user_content_len or 0,
                'agent_count': len(agent_messages),
                'agent_total_length': total_content_length - (user_content_len or 0),
                'agent_text_length': agent_text_length,
                'agent_timestamps': agent_timestamps
            })
        
        return tasks
    
    def _is_error_message(self, summary: str) -> bool:
        """Check if summary is an error message."""
        error_patterns = [
            "The string to replace was not found in the file.",
            "Invalid: old_string and new_string are exactly the same.",
            "Cancelled",
        ]
        return any(err in summary for err in error_patterns)
    
    def _normalize_summary(self, summary: str) -> str:
        """Normalize summary for duplicate detection.
        For file operations, extract file path to detect same file operations.
        """
        # For file operations, normalize by file path
        if 'Read file:' in summary:
            try:
                file_path = summary.split('Read file:')[1].split(' •')[0].strip()
                tool_type = summary.split('**')[1] if '**' in summary else 'read_file'
                return f"{tool_type}:{file_path}"
            except:
                return summary
        elif 'Edit file:' in summary:
            try:
                file_path = summary.split('Edit file:')[1].split(' •')[0].strip()
                tool_type = summary.split('**')[1] if '**' in summary else 'code_edit'
                return f"{tool_type}:{file_path}"
            except:
                return summary
        # For other summaries, use as-is
        return summary
    
    def _filter_summaries(self, summaries: List[str]) -> List[str]:
        """Hybrid deduplication: window-based + file operation consolidation + special handling."""
        # Step 1: Remove error messages
        filtered = []
        for summary in summaries:
            if not self._is_error_message(summary):
                filtered.append(summary)
        
        if not filtered:
            return []
        
        # Step 2: Window-based deduplication
        window_size = 5
        deduplicated = []
        seen_in_window = {}  # normalized_summary -> last position
        
        for i, summary in enumerate(filtered):
            normalized = self._normalize_summary(summary)
            last_seen = seen_in_window.get(normalized, -window_size - 1)
            
            # Skip if duplicate in window (unless it's a command tool)
            if i - last_seen <= window_size:
                # Special handling: commands might be different even with similar summaries
                if '**command**' in summary:
                    # Keep commands but still mark as seen to prevent exact duplicates
                    if summary == (deduplicated[last_seen] if last_seen >= 0 and last_seen < len(deduplicated) else None):
                        continue
                else:
                    continue
            
            seen_in_window[normalized] = len(deduplicated)
            deduplicated.append(summary)
        
        # Step 3: File operation consolidation (consolidate rapid re-reads/edits)
        if not deduplicated:
            return []
        
        consolidated = []
        file_ops_tracker = {}  # file_path -> (last_index, last_type, count)
        window_size_file = 5
        
        for i, summary in enumerate(deduplicated):
            normalized = self._normalize_summary(summary)
            
            # Check if it's a file operation
            is_file_op = 'Read file:' in summary or 'Edit file:' in summary
            
            if is_file_op:
                # Extract file path
                try:
                    if 'Read file:' in summary:
                        file_path = summary.split('Read file:')[1].split(' •')[0].strip()
                        op_type = 'read'
                    else:
                        file_path = summary.split('Edit file:')[1].split(' •')[0].strip()
                        op_type = 'edit'
                    
                    # Check if same file was accessed recently
                    if file_path in file_ops_tracker:
                        last_idx, last_type, count = file_ops_tracker[file_path]
                        gap = i - last_idx
                        
                        # If same file, same operation type, within window, consolidate
                        if gap <= window_size_file and op_type == last_type:
                            # Update count but skip adding duplicate
                            file_ops_tracker[file_path] = (i, op_type, count + 1)
                            # Optionally add a note about consolidation
                            # For now, just skip the duplicate
                            continue
                        else:
                            # Different operation or outside window, reset
                            file_ops_tracker[file_path] = (i, op_type, 1)
                    else:
                        # First time seeing this file
                        file_ops_tracker[file_path] = (i, op_type, 1)
                except:
                    # If extraction fails, keep the summary as-is
                    pass
            
            consolidated.append(summary)
        
        # Step 4: Special handling for todo_write (keep first, note frequency if >3)
        final = []
        todo_write_seen = False
        todo_write_count = 0
        
        for summary in consolidated:
            if '**todo_write**' in summary or 'Todo List' in summary:
                if not todo_write_seen:
                    todo_write_seen = True
                    todo_write_count = sum(1 for s in consolidated if '**todo_write**' in s or 'Todo List' in s)
                    if todo_write_count > 3:
                        # Add count to first occurrence
                        summary_with_count = summary.rstrip('.') + f" (×{todo_write_count})"
                        final.append(summary_with_count)
                    else:
                        final.append(summary)
                    # Skip subsequent todo_write occurrences
                    continue
                else:
                    # Already added first todo_write, skip
                    continue
            
            final.append(summary)
        
        return final
    
    def format_task_text(self, user_content: str, agent_summaries: List[str]) -> str:
        parts = [f"User: {user_content}"]
        for summary in agent_summaries:
            parts.append(f"Agent: {summary}")
        return "\n".join(parts)
    
    def close(self):
        self.chats_conn.close()


def test_task_lengths(db_path: str):
    builder = TaskBuilder(db_path)
    tasks = builder.get_message_tasks()
    
    print(f"Total tasks: {len(tasks)}\n")
    
    lengths = [s['formatted_length'] for s in tasks]
    
    print(f"Task length statistics:")
    print(f"  Min: {min(lengths):,} chars")
    print(f"  Max: {max(lengths):,} chars")
    print(f"  Avg: {sum(lengths)/len(lengths):,.0f} chars")
    print(f"  Median: {sorted(lengths)[len(lengths)//2]:,} chars")
    
    over_15k = [s for s in tasks if s['formatted_length'] > 15000]
    print(f"\nTasks over 15k chars: {len(over_15k)}")
    
    if over_15k:
        print("\nTasks exceeding 15k char limit:")
        for task in sorted(over_15k, key=lambda x: x['formatted_length'], reverse=True)[:5]:
            print(f"\n{'='*80}")
            print(f"Seq {task['user_msg_id']}: {task['formatted_length']:,} chars, {task['message_count']} messages")
            print(f"  User content: {len(task['user_content']):,} chars")
            print(f"  Agent summaries count: {len(task['agent_summaries'])}")
            total_agent_len = sum(len(s) for s in task['agent_summaries'])
            print(f"  Total agent summaries length: {total_agent_len:,} chars")
            
            agent_summary_lengths = [len(s) for s in task['agent_summaries']]
            if agent_summary_lengths:
                print(f"  Agent summary lengths - min: {min(agent_summary_lengths):,}, max: {max(agent_summary_lengths):,}, avg: {sum(agent_summary_lengths)/len(agent_summary_lengths):,.0f}")
            
            print(f"\nFirst 30 lines of formatted task:")
            print("-" * 80)
            lines = task['formatted_text'].split('\n')
            for i, line in enumerate(lines[:30], 1):
                print(f"{i:3}: {line[:150]}")
            if len(lines) > 30:
                print(f"... ({len(lines) - 30} more lines)")
            print("-" * 80)
    
    print("\n" + "="*80)
    print("Example of a medium-length task:")
    print("="*80)
    medium_tasks = [s for s in tasks if 1000 <= s['formatted_length'] <= 5000]
    if medium_tasks:
        task = medium_tasks[len(medium_tasks)//2]
        print(f"Seq {task['user_msg_id']}: {task['formatted_length']:,} chars, {task['message_count']} messages\n")
        lines = task['formatted_text'].split('\n')
        for i, line in enumerate(lines[:50], 1):
            print(f"{i:3}: {line}")
        if len(lines) > 50:
            print(f"... ({len(lines) - 50} more lines)")
    
    print("\n" + "="*80)
    print("Example of a short task:")
    print("="*80)
    short_tasks = [s for s in tasks if s['formatted_length'] < 500]
    if short_tasks:
        task = short_tasks[len(short_tasks)//2]
        print(f"Seq {task['user_msg_id']}: {task['formatted_length']:,} chars, {task['message_count']} messages\n")
        print(task['formatted_text'])
    
    over_300_summaries = []
    for task in tasks:
        for idx, summary in enumerate(task['agent_summaries']):
            if len(summary) > 300:
                over_300_summaries.append((task['user_msg_id'], idx, len(summary)))
    
    if over_300_summaries:
        print(f"\n{'='*80}")
        print(f"Agent summaries over 300 chars: {len(over_300_summaries)}")
        print(f"Top 5 longest summaries:")
        for user_msg_id, summary_idx, length in sorted(over_300_summaries, key=lambda x: x[2], reverse=True)[:5]:
            task = next(s for s in tasks if s['user_msg_id'] == user_msg_id)
            summary = task['agent_summaries'][summary_idx]
            print(f"\n  Seq {user_msg_id}, summary #{summary_idx}: {length} chars")
            print(f"  Preview (first 200 chars): {summary[:200]}...")
    
    builder.close()
    return len(over_15k) == 0


if __name__ == '__main__':
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='Test task building and lengths')
    parser.add_argument('--db-file', default='cursor-chats.db', help='Path to database file')
    
    args = parser.parse_args()
    
    all_sane = test_task_lengths(args.db_file)
    sys.exit(0 if all_sane else 1)

