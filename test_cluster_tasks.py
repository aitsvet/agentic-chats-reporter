#!/usr/bin/env python3
import subprocess
import sys
import os
import sqlite3
from pathlib import Path


def test_cluster_tasks():
    chats_db = 'EXAMPLE.db'
    
    if not os.path.exists(chats_db):
        print(f"ERROR: Chats database {chats_db} does not exist")
        print("Please run test_parse_chats.py and test_embed_tasks.py first to create it and embeddings")
        return False
    
    conn = sqlite3.connect(chats_db)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM task_embeddings")
    embedding_count = cursor.fetchone()[0]
    
    if embedding_count == 0:
        print("ERROR: No embeddings found in database")
        print("Please run test_embed_tasks.py first to generate embeddings")
        conn.close()
        return False
    
    print(f"Found {embedding_count} embeddings in database")
    
    cursor.execute("DROP TABLE IF EXISTS task_groups")
    conn.commit()
    conn.close()
    
    print(f"Using chats database: {chats_db} (clean groups table)")
    
    print("Running cluster_tasks.py (using .env for EMB_URL, EMB_MODEL, LLM_URL, LLM_MODEL)...")
    result = subprocess.run(
        [sys.executable, 'cluster_tasks.py', '--chats-db', chats_db],
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
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_groups'")
    if not cursor.fetchone():
        print("ERROR: task_groups table not created")
        conn.close()
        return False
    
    cursor.execute("SELECT COUNT(DISTINCT group_id) FROM task_groups WHERE threshold = -1.0")
    total_groups = cursor.fetchone()[0]
    print(f"Total groups stored: {total_groups}")
    
    assert total_groups > 0, "Expected more than 0 groups to be created"
    
    cursor.execute("SELECT COUNT(*) FROM task_groups WHERE threshold = -1.0")
    total_grouped_tasks = cursor.fetchone()[0]
    print(f"Total tasks in groups: {total_grouped_tasks}")
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'User' AND message_datetime IS NOT NULL")
    user_msg_count = cursor.fetchone()[0]
    
    assert total_grouped_tasks == user_msg_count, \
        f"Expected all {user_msg_count} user messages to be grouped, but found {total_grouped_tasks}"
    
    cursor.execute("""
        SELECT MAX(group_sum)
        FROM (
            SELECT sg.group_id, COALESCE(SUM(se.formatted_length), 0) as group_sum
            FROM task_groups sg
            LEFT JOIN task_embeddings se ON sg.user_msg_id = se.user_msg_id
            WHERE sg.threshold = -1.0
            GROUP BY sg.group_id
        )
    """)
    max_group_length = cursor.fetchone()[0]
    print(f"Max group length: {max_group_length}")
    
    assert max_group_length <= 25600 + 1000, \
        f"Max group length {max_group_length} exceeds expected LLM context limit (25600)"
    
    conn.close()
    
    print("\nChecking output report...")
    output_lines = result.stdout.split('\n')
    
    has_total_tasks = any('Total Tasks' in line for line in output_lines)
    assert has_total_tasks, "Output should contain 'Total Tasks'"
    
    has_clustering_stats = any('Clustering Statistics' in line for line in output_lines)
    assert has_clustering_stats, "Output should contain 'Clustering Statistics'"
        
    has_stats_table = any('| Metric | Value |' in line for line in output_lines)
    assert has_stats_table, "Output should contain clustering stats table"
    
    print("\n✓ All tests passed!")
    print(f"  - Created {total_groups} groups")
    print(f"  - Grouped {total_grouped_tasks} tasks")
    print(f"  - Max group length {max_group_length} is within LLM context limit")
    
    return True


if __name__ == '__main__':
    success = test_cluster_tasks()
    sys.exit(0 if success else 1)

