#!/usr/bin/env python3
import subprocess
import sys
import os
import sqlite3
from pathlib import Path


def test_embed_tasks():
    chats_db = 'EXAMPLE.db'
    
    if not os.path.exists(chats_db):
        print(f"ERROR: Chats database {chats_db} does not exist")
        print("Please run test_parse_chats.py first to create it")
        return False
    
    conn = sqlite3.connect(chats_db)
    cursor = conn.cursor()
    
    cursor.execute("DROP TABLE IF EXISTS task_embeddings")
    cursor.execute("DROP TABLE IF EXISTS task_groups")
    conn.commit()
    conn.close()
    
    print(f"Using chats database: {chats_db} (clean embeddings tables)")
    
    print("Running embed_tasks.py (using .env for EMB_URL and EMB_MODEL)...")
    result = subprocess.run(
        [sys.executable, 'embed_tasks.py', '--chats-db', chats_db],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent
    )
        
    if result.returncode != 0:
        print("ERROR: Script failed:")
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        return False
    
    print("Checking database tables...")
    conn = sqlite3.connect(chats_db)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_embeddings'")
    if not cursor.fetchone():
        print("ERROR: task_embeddings table not created")
        conn.close()
        return False
    
    cursor.execute("SELECT COUNT(*) FROM task_embeddings")
    embedding_count = cursor.fetchone()[0]
    print(f"Embeddings stored: {embedding_count}")
    
    if embedding_count == 0:
        print("ERROR: No embeddings were stored")
        print("This likely means the embedding API (EMB_URL) is not accessible or returned errors.")
        print("Please check your .env file and verify EMB_URL and EMB_MODEL are correct.")
        conn.close()
        return False
    
    cursor.execute("SELECT user_msg_id, message_count, formatted_length FROM task_embeddings LIMIT 5")
    samples = cursor.fetchall()
    print(f"Sample embeddings: {samples}")
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'User' AND message_datetime IS NOT NULL")
    user_msg_count = cursor.fetchone()[0]
    
    if embedding_count != user_msg_count:
        print(f"WARNING: Expected {user_msg_count} embeddings for {user_msg_count} user messages, got {embedding_count}")
    
    cursor.execute("SELECT COUNT(*) FROM task_embeddings WHERE formatted_length IS NOT NULL")
    length_count = cursor.fetchone()[0]
    assert length_count == embedding_count, f"Expected all embeddings to have formatted_length, got {length_count}/{embedding_count}"
    
    conn.close()
    
    print("\nChecking output...")
    output_lines = result.stdout.split('\n')
    
    has_found_tasks = any('Found' in line and 'user message tasks' in line for line in output_lines)
    assert has_found_tasks, "Output should contain 'Found X user message tasks'"
    
    has_embedding_complete = any('Embedding extraction complete' in line for line in output_lines)
    assert has_embedding_complete, "Output should contain 'Embedding extraction complete'"
    
    print("\n✓ All tests passed!")
    print(f"  - Stored {embedding_count} embeddings")
    print(f"  - All embeddings have formatted_length")
    
    return True


if __name__ == '__main__':
    success = test_embed_tasks()
    sys.exit(0 if success else 1)

