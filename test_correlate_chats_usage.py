#!/usr/bin/env python3
import subprocess
import tempfile
import os
import sqlite3
from pathlib import Path

def test_example_correlation():
    with tempfile.TemporaryDirectory() as tmpdir:
        chats_db = os.path.join(tmpdir, 'example-chats.db')
        usage_db = os.path.join(tmpdir, 'example-usage.db')
        
        chats_conn = sqlite3.connect(chats_db)
        chats_cursor = chats_conn.cursor()
        
        chats_cursor.execute("""
            CREATE TABLE chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                chat_datetime TEXT,
                start_line INTEGER,
                end_line INTEGER
            )
        """)
        
        chats_cursor.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message_type TEXT,
                message_datetime TEXT,
                summary TEXT,
                data_tool_type TEXT,
                data_tool_name TEXT,
                content_type TEXT,
                content_length INTEGER,
                start_line INTEGER,
                end_line INTEGER,
                FOREIGN KEY (chat_id) REFERENCES chats(id)
            )
        """)
        
        chats_cursor.execute("""
            CREATE TABLE content (
                message_id INTEGER PRIMARY KEY,
                content_text TEXT,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        """)
        
        chat_data = [
            ('Update document upload and processing flow', '2025-09-29 20:11Z', [
                ('User', '2025-09-29 18:42Z', 200),
                ('Agent', None, 1500),
                ('Agent', None, 800),
            ]),
            ('Current users and roles in PostgreSQL', '2025-10-26 18:59Z', [
                ('User', '2025-10-26 06:34Z', 100),
                ('Agent', None, 800),
            ]),
            ('Find and verify import endpoints', '2025-10-19 11:33Z', [
                ('User', '2025-10-19 10:26Z', 150),
                ('Agent', None, 1200),
            ]),
            ('Convert the AGENTS.md file to PDF', '2025-10-26 22:21Z', [
                ('User', '2025-10-26 06:34Z', 50),
                ('Agent', None, 600),
            ]),
            ('Fix checkmark toggle in accordion', '2025-10-06 21:25Z', [
                ('User', '2025-10-06 17:24Z', 120),
                ('Agent', None, 1000),
            ]),
        ]
        
        for chat_title, chat_dt, messages in chat_data:
            chats_cursor.execute(
                "INSERT INTO chats (title, chat_datetime) VALUES (?, ?)",
                (chat_title, chat_dt)
            )
            chat_id = chats_cursor.lastrowid
            
            for msg_type, msg_dt, content_len in messages:
                chats_cursor.execute(
                    "INSERT INTO messages (chat_id, message_type, message_datetime, content_length) VALUES (?, ?, ?, ?)",
                    (chat_id, msg_type, msg_dt, content_len)
                )
                msg_id = chats_cursor.lastrowid
                chats_cursor.execute(
                    "INSERT INTO content (message_id, content_text) VALUES (?, ?)",
                    (msg_id, 'x' * content_len)
                )
        
        chats_conn.commit()
        chats_conn.close()
        
        usage_conn = sqlite3.connect(usage_db)
        usage_cursor = usage_conn.cursor()
        
        usage_cursor.execute("""
            CREATE TABLE usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                kind TEXT,
                model TEXT,
                max_mode TEXT,
                input_with_cache_write INTEGER,
                input_without_cache_write INTEGER,
                cache_read INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cost REAL,
                timestamp REAL
            )
        """)
        
        script_dir = Path(__file__).parent
        example_csv = script_dir / 'EXAMPLE.csv'
        
        import csv
        from datetime import datetime, timezone
        
        with open(example_csv, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_str = row['Date'].strip('"')
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                timestamp = dt.timestamp()
                
                usage_cursor.execute("""
                    INSERT INTO usage (date, kind, model, max_mode,
                        input_with_cache_write, input_without_cache_write, cache_read,
                        output_tokens, total_tokens, cost, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    date_str,
                    row['Kind'].strip('"'),
                    row['Model'].strip('"'),
                    row['Max Mode'].strip('"'),
                    int(row['Input (w/ Cache Write)']),
                    int(row['Input (w/o Cache Write)']),
                    int(row['Cache Read']),
                    int(row['Output Tokens']),
                    int(row['Total Tokens']),
                    float(row['Cost']),
                    timestamp
                ))
        
        usage_conn.commit()
        usage_conn.close()
        
        result = subprocess.run(
            ['python3', 'correlate_chats_usage.py', 
             '--chats-db', chats_db, '--usage-db', usage_db],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent
        )
        
        if result.returncode != 0:
            print("Script error:")
            print(result.stderr)
            print("Script output:")
            print(result.stdout)
            raise AssertionError(f"Script failed with return code {result.returncode}")
        
        output = result.stdout
        
        if 'Total' not in output:
            print("Full output:")
            print(output)
            raise AssertionError("Total line not found in output")
        
        expected_matches = {
            '2025-09-29': (1, 1),
            '2025-10-06': (1, 1),
            '2025-10-19': (1, 1),
            '2025-10-26': (2, 1),
        }
        
        lines = output.split('\n')
        daily_section = False
        total_line = None
        
        for line in lines:
            if '### Daily Statistics' in line:
                daily_section = True
                continue
            if daily_section and '| **Total**' in line:
                total_line = line
                break
        
        assert total_line is not None, "Total line not found in output"
        
        parts = [p.strip() for p in total_line.split('|')]
        assert len(parts) >= 10, f"Expected at least 10 columns in total line, got {len(parts)}. Line: {total_line}"
        
        def extract_number(s):
            s = s.replace('**', '').strip()
            if '(' in s:
                s = s.split('(')[0].strip()
            return int(s)
        
        total_chats = extract_number(parts[2])
        total_matched = extract_number(parts[3])
        total_unmatched = extract_number(parts[4])
        total_usage = extract_number(parts[5])
        total_matched_usage = extract_number(parts[6])
        total_unmatched_usage = extract_number(parts[7])
        
        assert total_chats == 5, f"Expected 5 chats, got {total_chats}"
        assert total_usage == 12, f"Expected 12 usage requests from EXAMPLE.csv, got {total_usage}"
        assert total_matched == 5, f"Expected all 5 chats to match, got {total_matched}"
        assert total_matched_usage == 12, f"Expected all 12 usage requests to match, got {total_matched_usage}"
        
        print("✓ Test passed!")
        print(f"  Total chats: {total_chats}")
        print(f"  Matched chats: {total_matched}")
        print(f"  Total usage requests: {total_usage}")
        print(f"  Matched usage requests: {total_matched_usage}")
        
        return True

if __name__ == '__main__':
    test_example_correlation()

