#!/usr/bin/env python3
import sqlite3
import subprocess
import sys
import os


def test_parse_usage():
    csv_file = 'EXAMPLE.csv'
    db_file = 'EXAMPLE.db'
    
    result = subprocess.run(
        [sys.executable, 'parse_usage.py', csv_file, '--db', db_file],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print(f"ERROR: Parser failed with return code {result.returncode}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        return False
    
    if not os.path.exists(db_file):
        print(f"ERROR: Database file {db_file} was not created")
        return False
    
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM usage")
    usage_count = cursor.fetchone()[0]
    print(f"Usage records: {usage_count}")
    assert usage_count == 12, f"Expected 12 records, got {usage_count}"
    
    cursor.execute("SELECT SUM(total_tokens) FROM usage")
    total_tokens = cursor.fetchone()[0]
    print(f"Total tokens: {total_tokens:,}")
    assert total_tokens == 8676636, f"Expected 8676636 total tokens, got {total_tokens}"
    
    cursor.execute("SELECT COUNT(*) FROM usage WHERE cache_read > 0")
    cache_read_count = cursor.fetchone()[0]
    print(f"Records with cache read: {cache_read_count}")
    assert cache_read_count >= 11, f"Expected at least 11 records with cache read, got {cache_read_count}"
    
    cursor.execute("SELECT SUM(input_without_cache_write) FROM usage")
    input_wo_cache = cursor.fetchone()[0]
    print(f"Total input w/o cache write: {input_wo_cache:,}")
    assert input_wo_cache == 0, f"Expected 0 input w/o cache write tokens, got {input_wo_cache}"
    
    cursor.execute("SELECT AVG(total_tokens) FROM usage")
    avg_tokens = cursor.fetchone()[0]
    print(f"Avg tokens per request: {avg_tokens:,.2f}")
    assert abs(avg_tokens - 723053.0) < 1.0, f"Expected avg around 723053.0, got {avg_tokens}"
    
    output_lines = result.stdout.split('\n')
    has_table = any('Metric' in line and 'Total' in line for line in output_lines)
    assert has_table, "Output should contain statistics table with Metric and Total columns"
    
    has_tps = any('TPS' in line for line in output_lines)
    assert has_tps, "Output should contain TPS statistics"
    
    conn.close()
    print("All tests passed!")
    return True


if __name__ == '__main__':
    success = test_parse_usage()
    sys.exit(0 if success else 1)

