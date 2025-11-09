#!/usr/bin/env python3
import sqlite3
import argparse
import sys
from typing import List, Dict, Tuple
from collections import defaultdict
import glob
import os


def parse_chat_datetime(dt_str: str):
    """Parse datetime string from database."""
    if not dt_str:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except:
        return None


def analyze_group_continuity(db_path: str):
    """Analyze how consecutive tasks are within groups."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    all_tasks = cursor.execute("""
        SELECT m.id, m.message_datetime, m.start_line, m.chat_id
        FROM messages m
        WHERE m.message_type = 'User' AND m.message_datetime IS NOT NULL
        ORDER BY m.message_datetime, m.start_line
    """).fetchall()
    
    task_to_global_index = {task_id: idx for idx, (task_id, _, _, _) in enumerate(all_tasks)}
    
    groups = cursor.execute("""
        SELECT tg.group_id, tg.user_msg_id, m.message_datetime, m.start_line, m.chat_id
        FROM task_groups tg
        JOIN messages m ON tg.user_msg_id = m.id
        WHERE tg.threshold = -1.0
        ORDER BY tg.group_id, m.message_datetime, m.start_line
    """).fetchall()
    
    if not groups:
        print("No groups found in database")
        return
    
    group_tasks = defaultdict(list)
    for group_id, user_msg_id, msg_datetime, start_line, chat_id in groups:
        global_idx = task_to_global_index.get(user_msg_id, -1)
        group_tasks[group_id].append((user_msg_id, msg_datetime, start_line, chat_id, global_idx))
    
    print(f"Analyzing {len(group_tasks)} groups with {sum(len(tasks) for tasks in group_tasks.values())} total tasks\n")
    
    group_stats = []
    for group_id, tasks in sorted(group_tasks.items()):
        if len(tasks) < 2:
            continue
        
        tasks_sorted = sorted(tasks, key=lambda x: (x[4] if x[4] >= 0 else 999999))
        
        gaps = []
        consecutive_runs = []
        current_run = [tasks_sorted[0]]
        
        for i in range(1, len(tasks_sorted)):
            prev_idx = tasks_sorted[i-1][4]
            curr_idx = tasks_sorted[i][4]
            
            if prev_idx >= 0 and curr_idx >= 0:
                gap = curr_idx - prev_idx
                gaps.append(gap)
                
                if gap == 1:
                    current_run.append(tasks_sorted[i])
                else:
                    if len(current_run) > 1:
                        consecutive_runs.append(len(current_run))
                    current_run = [tasks_sorted[i]]
            else:
                if len(current_run) > 1:
                    consecutive_runs.append(len(current_run))
                current_run = [tasks_sorted[i]]
        
        if len(current_run) > 1:
            consecutive_runs.append(len(current_run))
        
        if gaps:
            avg_gap = sum(gaps) / len(gaps)
            max_gap = max(gaps)
            min_gap = min(gaps)
        else:
            avg_gap = max_gap = min_gap = 0
        
        total_consecutive = sum(consecutive_runs) if consecutive_runs else 0
        continuity_ratio = total_consecutive / len(tasks) if tasks else 0
        
        group_stats.append({
            'group_id': group_id,
            'task_count': len(tasks),
            'avg_gap': avg_gap,
            'min_gap': min_gap,
            'max_gap': max_gap,
            'consecutive_runs': consecutive_runs,
            'continuity_ratio': continuity_ratio,
            'tasks': tasks_sorted
        })
    
    group_titles = {}
    titles_data = cursor.execute("""
        SELECT group_id, title
        FROM group_summaries
    """).fetchall()
    for group_id, title in titles_data:
        group_titles[group_id] = title
    
    for stats in group_stats:
        stats['title'] = group_titles.get(stats['group_id'], '(no title)')
    
    group_stats.sort(key=lambda x: x['continuity_ratio'])
    
    print("=" * 80)
    print("GROUP continuity ANALYSIS")
    print("=" * 80)
    print()
    
    print("Summary Statistics:")
    print("-" * 80)
    if group_stats:
        avg_continuity = sum(s['continuity_ratio'] for s in group_stats) / len(group_stats)
        avg_gap = sum(s['avg_gap'] for s in group_stats) / len(group_stats)
        total_groups = len(group_stats)
        highly_consecutive = sum(1 for s in group_stats if s['continuity_ratio'] >= 0.8)
        moderately_consecutive = sum(1 for s in group_stats if 0.5 <= s['continuity_ratio'] < 0.8)
        low_consecutive = sum(1 for s in group_stats if s['continuity_ratio'] < 0.5)
        
        print(f"Total groups analyzed: {total_groups}")
        print(f"Average continuity ratio: {avg_continuity:.2%}")
        print(f"Average gap between tasks: {avg_gap:.1f} tasks")
        print(f"Highly consecutive groups (≥80%): {highly_consecutive} ({100*highly_consecutive/total_groups:.1f}%)")
        print(f"Moderately consecutive groups (50-80%): {moderately_consecutive} ({100*moderately_consecutive/total_groups:.1f}%)")
        print(f"Low consecutive groups (<50%): {low_consecutive} ({100*low_consecutive/total_groups:.1f}%)")
    print()
    
    print("Top 10 Most Consecutive Groups:")
    print("-" * 80)
    for i, stats in enumerate(group_stats[-10:][::-1], 1):
        print(f"{i}. Group {stats['group_id']}: {stats['task_count']} tasks, "
              f"{stats['continuity_ratio']:.1%} consecutive, "
              f"avg gap: {stats['avg_gap']:.1f}, max gap: {stats['max_gap']}")
        print(f"   Title: {stats['title']}")
        if stats['consecutive_runs']:
            print(f"   Consecutive runs: {stats['consecutive_runs']}")
    print()
    
    print("20 Least Consecutive Groups (with titles and user task summaries):")
    print("-" * 80)
    for i, stats in enumerate(group_stats[:20], 1):
        print(f"{i}. Group {stats['group_id']}: {stats['task_count']} tasks, "
              f"{stats['continuity_ratio']:.1%} consecutive, "
              f"avg gap: {stats['avg_gap']:.1f}, max gap: {stats['max_gap']}")
        print(f"   Title: {stats['title']}")
        if stats['consecutive_runs']:
            print(f"   Consecutive runs: {stats['consecutive_runs']}")
        else:
            print(f"   No consecutive runs (all tasks are separated)")
        
        user_msg_ids = [t[0] for t in stats['tasks']]
        if user_msg_ids:
            placeholders = ','.join(['?'] * len(user_msg_ids))
            user_data = cursor.execute(f"""
                SELECT m.id, m.summary, c.content_text
                FROM messages m
                LEFT JOIN content c ON m.id = c.message_id
                WHERE m.id IN ({placeholders})
            """, user_msg_ids).fetchall()
            
            user_data_dict = {msg_id: (summary, content_text) for msg_id, summary, content_text in user_data}
            
            print(f"   User task summaries (first line each):")
            for msg_id in user_msg_ids:
                summary, content_text = user_data_dict.get(msg_id, (None, None))
                
                first_line = None
                if content_text:
                    for line in content_text.split('\n'):
                        stripped = line.strip()
                        if stripped:
                            first_line = stripped
                            break
                
                if not first_line and summary:
                    first_line = summary
                
                if first_line:
                    print(f"     - {first_line[:120]}{'...' if len(first_line) > 120 else ''}")
                else:
                    print(f"     - (no content or summary)")
        print()
    
    print("Detailed View - Sample Groups (showing task indices):")
    print("-" * 80)
    print("\nMost consecutive group example:")
    if group_stats:
        best = group_stats[-1]
        print(f"Group {best['group_id']} ({best['task_count']} tasks, {best['continuity_ratio']:.1%} consecutive):")
        task_indices = [t[4] for t in best['tasks'] if t[4] >= 0]
        if task_indices:
            print(f"  Task indices: {task_indices[:20]}{'...' if len(task_indices) > 20 else ''}")
            if len(task_indices) > 1:
                gaps = [task_indices[i] - task_indices[i-1] for i in range(1, len(task_indices))]
                print(f"  Gaps: {gaps[:20]}{'...' if len(gaps) > 20 else ''}")
    
    print("\nLeast consecutive group example:")
    if group_stats:
        worst = group_stats[0]
        print(f"Group {worst['group_id']} ({worst['task_count']} tasks, {worst['continuity_ratio']:.1%} consecutive):")
        task_indices = [t[4] for t in worst['tasks'] if t[4] >= 0]
        if task_indices:
            print(f"  Task indices: {task_indices[:20]}{'...' if len(task_indices) > 20 else ''}")
            if len(task_indices) > 1:
                gaps = [task_indices[i] - task_indices[i-1] for i in range(1, len(task_indices))]
                print(f"  Gaps: {gaps[:20]}{'...' if len(gaps) > 20 else ''}")
    
    conn.close()


def main():
    parser = argparse.ArgumentParser(description='Analyze how consecutive tasks are within groups')
    parser.add_argument('--db-file', default=None, help='Path to database file (default: searches for *.db files, uses most recent)')
    
    args = parser.parse_args()
    
    chats_db = args.db_file
    if chats_db is None:
        db_files = glob.glob('*.db')
        if not db_files:
            raise ValueError("No database files found. Please specify --db-file")
        chats_db = max(db_files, key=os.path.getmtime)
        print(f"Using most recent database: {chats_db}\n")
    
    analyze_group_continuity(chats_db)


if __name__ == '__main__':
    main()

