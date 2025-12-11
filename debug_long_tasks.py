#!/usr/bin/env python3
import sqlite3
import argparse
from db_utils import find_db_file


def analyze_long_tasks(db_path: str, limit: int = 3):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    tasks_with_lengths = cursor.execute("""
        SELECT se.user_msg_id, se.message_count,
               (SELECT SUM(LENGTH(c.content_text)) 
                FROM messages m 
                JOIN content c ON m.id = c.message_id 
                WHERE m.chat_id = (SELECT chat_id FROM messages WHERE id = se.user_msg_id)
                AND m.id >= se.user_msg_id
                AND m.id < COALESCE(
                    (SELECT MIN(id) FROM messages 
                     WHERE chat_id = (SELECT chat_id FROM messages WHERE id = se.user_msg_id) 
                     AND message_type = 'User' AND id > se.user_msg_id),
                    999999999
                )) as total_length
        FROM task_embeddings se
        ORDER BY total_length DESC
        LIMIT ?
    """, (limit,)).fetchall()
    
    if not tasks_with_lengths:
        print("No tasks found in task_embeddings table")
        return
    
    print(f"Analyzing {len(tasks_with_lengths)} longest tasks:\n")
    
    for user_msg_id, msg_count, total_length in tasks_with_lengths:
        print("=" * 80)
        print(f"Task {user_msg_id}: {msg_count} messages, {total_length:,} total characters\n")
        
        chat_id = cursor.execute("""
            SELECT chat_id FROM messages WHERE id = ?
        """, (user_msg_id,)).fetchone()[0]
        
        messages = cursor.execute("""
            SELECT m.id, m.message_type, m.content_type, m.content_length, m.summary,
                   c.content_text
            FROM messages m
            LEFT JOIN content c ON m.id = c.message_id
            WHERE m.chat_id = ?
            AND m.id >= ?
            AND m.id < COALESCE(
                (SELECT MIN(id) FROM messages 
                 WHERE chat_id = ? AND message_type = 'User' AND id > ?),
                999999999
            )
            ORDER BY m.message_datetime, m.start_line
        """, (chat_id, user_msg_id, chat_id, user_msg_id)).fetchall()
        
        print("Messages in task:")
        print("-" * 80)
        message_lengths = []
        for msg_id, msg_type, content_type, content_len, summary, content_text in messages:
            actual_len = len(content_text) if content_text else (content_len or 0)
            message_lengths.append((msg_id, msg_type, content_type, actual_len, summary))
            print(f"  Message {msg_id}: {msg_type}, type={content_type}, length={actual_len:,}, summary={summary[:60] if summary else 'None'}")
        
        print("\nTop 3 longest messages:")
        message_lengths.sort(key=lambda x: x[3], reverse=True)
        for idx, (msg_id, msg_type, content_type, length, summary) in enumerate(message_lengths[:3], 1):
            print(f"  {idx}. Message {msg_id}: {msg_type}, type={content_type}, length={length:,}")
            print(f"     Summary: {summary[:100] if summary else 'None'}...")
        
        print("\nFirst 20 lines of task content:")
        print("-" * 80)
        full_text_parts = []
        for msg_id, msg_type, content_type, content_len, summary, content_text in messages:
            if msg_type == 'User':
                if content_text:
                    full_text_parts.append(f"User: {content_text}")
            elif msg_type == 'Agent':
                if content_type == 'tool_call' and content_text:
                    import re
                    summary_match = re.search(r'<summary>(.*?)</summary>', content_text, re.DOTALL)
                    if summary_match:
                        full_text_parts.append(f"Agent: Tool: {summary_match.group(1).strip()}")
                elif summary:
                    full_text_parts.append(f"Agent: {summary}")
                elif content_len:
                    full_text_parts.append(f"Agent message ({content_len} chars)")
        
        full_text = "\n".join(full_text_parts)
        lines = full_text.split('\n')
        for i, line in enumerate(lines[:20], 1):
            print(f"{i:3}: {line[:150]}")
        if len(lines) > 20:
            print(f"... ({len(lines) - 20} more lines)")
        print()
    
    conn.close()


def main():
    parser = argparse.ArgumentParser(description='Debug long tasks')
    parser.add_argument('--db-file', default='EXAMPLE.db', help='Path to database file (default: EXAMPLE.db or searches for most recent *.db)')
    parser.add_argument('--limit', type=int, default=3, help='Number of longest tasks to analyze')
    
    args = parser.parse_args()
    chats_db = find_db_file(args.db_file if args.db_file != 'EXAMPLE.db' else None)
    analyze_long_tasks(chats_db, args.limit)


if __name__ == '__main__':
    main()

