#!/usr/bin/env python3
import sqlite3
import subprocess
import sys
import os


def test_parse_chats():
    md_file = 'EXAMPLE.md'
    db_file = 'EXAMPLE.db'
    
    if os.path.exists(db_file):
        os.remove(db_file)
    
    result = subprocess.run(
        [sys.executable, 'parse_chats.py', md_file, '--db', db_file],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print(f"ERROR: Parser failed with return code {result.returncode}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        return False
    
    stderr_lines = result.stderr.strip().split('\n') if result.stderr else []
    invalid_blocks = [line for line in stderr_lines if 'Invalid block' in line]
    if invalid_blocks:
        print("ERROR: Found invalid blocks:")
        for block in invalid_blocks:
            print(f"  {block}")
        return False
    
    if not os.path.exists(db_file):
        print(f"ERROR: Database file {db_file} was not created")
        return False
    
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM chats")
    chat_count = cursor.fetchone()[0]
    print(f"Chats: {chat_count}")
    assert chat_count == 5, f"Expected 5 chats, got {chat_count}"
    
    cursor.execute("SELECT id, title, chat_datetime FROM chats ORDER BY chat_datetime, start_line")
    chats = cursor.fetchall()
    print(f"Chat records: {chats}")
    
    cursor.execute("SELECT COUNT(*) FROM messages")
    message_count = cursor.fetchone()[0]
    print(f"Messages: {message_count}")
    assert message_count == 21, f"Expected 21 messages, got {message_count}"
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 1")
    chat1_messages = cursor.fetchone()[0]
    print(f"Chat 1 messages: {chat1_messages}")
    assert chat1_messages == 4, f"Expected 4 messages in chat 1 (1 User + 3 Agent), got {chat1_messages}"
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 2")
    chat2_messages = cursor.fetchone()[0]
    print(f"Chat 2 messages: {chat2_messages}")
    assert chat2_messages == 3, f"Expected 3 messages in chat 2 (1 User + 2 Agent), got {chat2_messages}"
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 3")
    chat3_messages = cursor.fetchone()[0]
    print(f"Chat 3 messages: {chat3_messages}")
    assert chat3_messages == 4, f"Expected 4 messages in chat 3 (1 User + 3 Agent), got {chat3_messages}"
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 4")
    chat4_messages = cursor.fetchone()[0]
    print(f"Chat 4 messages: {chat4_messages}")
    assert chat4_messages == 5, f"Expected 5 messages in chat 4 (1 User + 4 Agent), got {chat4_messages}"
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 5")
    chat5_messages = cursor.fetchone()[0]
    print(f"Chat 5 messages: {chat5_messages}")
    assert chat5_messages == 5, f"Expected 5 messages in chat 5 (1 User + 4 Agent), got {chat5_messages}"
    
    cursor.execute("""
        SELECT id, chat_id, message_type, message_datetime, summary, data_tool_type, data_tool_name 
        FROM messages 
        ORDER BY message_datetime, start_line
    """)
    messages = cursor.fetchall()
    print(f"Message records: {messages}")
    
    cursor.execute("SELECT COUNT(*) FROM content")
    content_count = cursor.fetchone()[0]
    print(f"Content blocks: {content_count}")
    
    cursor.execute("""
        SELECT c.message_id, LENGTH(c.content_text) as len 
        FROM content c
        JOIN messages m ON c.message_id = m.id
        ORDER BY m.message_datetime, m.start_line
    """)
    content = cursor.fetchall()
    print(f"Content records: {content}")
    
    cursor.execute("""
        SELECT m.id, m.message_type, m.message_datetime, 
               CASE WHEN c.message_id IS NOT NULL THEN 1 ELSE 0 END as has_content
        FROM messages m
        LEFT JOIN content c ON m.id = c.message_id
        ORDER BY m.message_datetime, m.start_line
    """)
    message_content_counts = cursor.fetchall()
    print(f"Messages with content: {message_content_counts}")
    
    for msg_id, msg_type, msg_dt, has_content in message_content_counts:
        assert has_content == 1, f"Message {msg_id} should have content"
    
    cursor.execute("""
        SELECT DISTINCT content_type FROM messages WHERE content_type IS NOT NULL
    """)
    content_types = [row[0] for row in cursor.fetchall()]
    print(f"Content types found: {content_types}")
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE data_tool_type IS NOT NULL
    """)
    tool_message_count = cursor.fetchone()[0]
    print(f"Messages with tool calls: {tool_message_count}")
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE summary IS NOT NULL
    """)
    summary_message_count = cursor.fetchone()[0]
    print(f"Messages with summaries: {summary_message_count}")
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE content_type = 'think'
    """)
    think_count = cursor.fetchone()[0]
    print(f"Think messages: {think_count}")
    assert think_count == 0, f"Expected 0 think messages, got {think_count}"
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE content_type = 'tool_call'
    """)
    tool_call_count = cursor.fetchone()[0]
    print(f"Tool call messages: {tool_call_count}")
    assert tool_call_count == 9, f"Expected 9 tool_call messages, got {tool_call_count}"
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE content_type = 'text'
    """)
    text_count = cursor.fetchone()[0]
    print(f"Text messages: {text_count}")
    assert text_count == 12, f"Expected 12 text messages, got {text_count}"
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE message_type = 'User'
    """)
    user_count = cursor.fetchone()[0]
    print(f"User messages: {user_count}")
    assert user_count == 5, f"Expected 5 user messages, got {user_count}"
    
    cursor.execute("""
        SELECT COUNT(*) FROM messages WHERE message_type = 'Agent'
    """)
    agent_count = cursor.fetchone()[0]
    print(f"Agent messages: {agent_count}")
    assert agent_count == 16, f"Expected 16 agent messages, got {agent_count}"
    
    cursor.execute("""
        SELECT DISTINCT data_tool_name FROM messages WHERE data_tool_name IS NOT NULL ORDER BY data_tool_name
    """)
    tool_names = [row[0] for row in cursor.fetchall()]
    print(f"Tool names found: {tool_names}")
    expected_tools = ['codebase_search', 'grep', 'read_file', 'run_terminal_cmd', 'todo_write']
    assert set(tool_names) == set(expected_tools), f"Expected tools {expected_tools}, got {tool_names}"
    
    conn.close()
    print("All tests passed!")
    return True


if __name__ == '__main__':
    success = test_parse_chats()
    sys.exit(0 if success else 1)

