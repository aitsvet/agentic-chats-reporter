#!/usr/bin/env python3
import sys
import os
import subprocess
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def check_env_vars():
    required = ['EMB_URL', 'EMB_MODEL', 'LLM_URL', 'LLM_MODEL']
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        print("Please set them in .env file or as environment variables")
        sys.exit(1)


def run_pipeline(md_file='EXAMPLE.md', csv_file='EXAMPLE.csv'):
    script_dir = Path(__file__).parent
    db_path = Path(md_file).with_suffix('.db')
    output_file = Path(md_file).stem + '-REPORT.md'
    
    print("=" * 80)
    print("Testing Full Pipeline")
    print("=" * 80)
    print(f"Input files:")
    print(f"  - Markdown: {md_file}")
    print(f"  - CSV: {csv_file}")
    print(f"Output:")
    print(f"  - Database: {db_path}")
    print(f"  - Report: {output_file}")
    print()
    
    if db_path.exists():
        print(f"Removing existing database: {db_path}")
        db_path.unlink()
    
    if Path(output_file).exists():
        print(f"Removing existing report: {output_file}")
        Path(output_file).unlink()
    
    print("Running main.py...")
    print("-" * 80)
    
    result = subprocess.run(
        [sys.executable, str(script_dir / 'main.py'), '--md-file', md_file, '--csv-file', csv_file],
        text=True,
        encoding='utf-8'
    )
    
    if result.returncode != 0:
        print(f"Pipeline failed with return code {result.returncode}", file=sys.stderr)
        return False
    
    print()
    print("-" * 80)
    print("Pipeline completed successfully!")
    print()
    
    return verify_results(db_path, output_file)


def verify_results(db_path, output_file):
    print("=" * 80)
    print("Verifying Results")
    print("=" * 80)
    
    success = True
    
    if not db_path.exists():
        print(f"ERROR: Database file not created: {db_path}")
        return False
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    checks = [
        ("chats table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='chats'"),
        ("messages table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"),
        ("content table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='content'"),
        ("usage table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='usage'"),
        ("task_embeddings table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='task_embeddings'"),
        ("task_groups table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='task_groups'"),
        ("group_summaries table exists", "SELECT name FROM sqlite_master WHERE type='table' AND name='group_summaries'"),
    ]
    
    for check_name, query in checks:
        cursor.execute(query)
        result = cursor.fetchone()
        if result:
            print(f"✓ {check_name}")
        else:
            print(f"✗ {check_name} - MISSING")
            success = False
    
    cursor.execute("SELECT COUNT(*) FROM chats")
    chat_count = cursor.fetchone()[0]
    print(f"  Chats: {chat_count}")
    
    cursor.execute("SELECT COUNT(*) FROM messages WHERE message_type = 'User' AND message_datetime IS NOT NULL")
    user_msg_count = cursor.fetchone()[0]
    print(f"  User messages: {user_msg_count}")
    
    try:
        cursor.execute("SELECT COUNT(*) FROM task_embeddings")
        embedding_count = cursor.fetchone()[0]
        print(f"  Embeddings: {embedding_count}")
        
        if embedding_count != user_msg_count:
            print(f"  ERROR: Embedding count ({embedding_count}) != user message count ({user_msg_count})")
            success = False
    except sqlite3.OperationalError:
        print(f"  WARNING: task_embeddings table does not exist yet (pipeline may have failed early)")
        embedding_count = 0
    
    try:
        cursor.execute("SELECT COUNT(DISTINCT group_id) FROM task_groups WHERE threshold = -1.0")
        group_count = cursor.fetchone()[0]
        print(f"  Groups: {group_count}")
    except sqlite3.OperationalError:
        print(f"  WARNING: task_groups table does not exist yet (pipeline may have failed early)")
        group_count = 0
    
    try:
        cursor.execute("SELECT COUNT(*) FROM group_summaries")
        summary_count = cursor.fetchone()[0]
        print(f"  Group summaries: {summary_count}")
        
        if group_count > 0 and summary_count == 0:
            print(f"  WARNING: Groups exist but no summaries found")
        
        if summary_count > 0 and summary_count != group_count:
            print(f"  WARNING: Summary count ({summary_count}) != group count ({group_count})")
    except sqlite3.OperationalError:
        print(f"  WARNING: group_summaries table does not exist yet (pipeline may have failed early)")
        summary_count = 0
    
    try:
        cursor.execute("SELECT COUNT(*) FROM usage")
        usage_count = cursor.fetchone()[0]
        print(f"  Usage records: {usage_count}")
    except sqlite3.OperationalError:
        print(f"  WARNING: usage table does not exist (CSV may not have been processed)")
        usage_count = 0
    
    conn.close()
    
    if not Path(output_file).exists():
        print(f"ERROR: Report file not created: {output_file}")
        success = False
    else:
        print(f"✓ Report file created: {output_file}")
        report_size = Path(output_file).stat().st_size
        print(f"  Report size: {report_size:,} bytes")
        
        with open(output_file, 'r', encoding='utf-8') as f:
            content = f.read()
            if '# Task Summaries' in content:
                print(f"✓ Report contains task summaries section")
            else:
                print(f"✗ Report missing task summaries section")
                success = False
            
            if '## Input Files' in content:
                print(f"✓ Report contains input file statistics")
            else:
                print(f"✗ Report missing input file statistics")
                success = False
            
            lines = content.split('\n')
            
            if '| Category | Count | Length | Avg Length |' in content:
                parsing_table_start = None
                for i, line in enumerate(lines):
                    if '| Category | Count | Length | Avg Length |' in line:
                        parsing_table_start = i
                        break
                
                if parsing_table_start is not None:
                    parsing_table_end = None
                    for i in range(parsing_table_start + 2, min(parsing_table_start + 50, len(lines))):
                        if lines[i].strip().startswith('|') and not lines[i].strip().startswith('|---'):
                            parsing_table_end = i
                            break
                    
                    if parsing_table_end is not None:
                        print(f"✓ Report contains parsing statistics with data rows")
                    else:
                        print(f"✗ Report has parsing statistics header but missing data rows")
                        success = False
                else:
                    print(f"✗ Report missing parsing statistics table")
                    success = False
            else:
                print(f"✗ Report missing parsing statistics section")
                success = False
            
            if '| Metric | Requests |' in content:
                usage_table_start = None
                for i, line in enumerate(lines):
                    if '| Metric | Requests |' in line:
                        usage_table_start = i
                        break
                
                if usage_table_start is not None:
                    usage_table_end = None
                    for i in range(usage_table_start + 2, min(usage_table_start + 50, len(lines))):
                        if lines[i].strip().startswith('|') and not lines[i].strip().startswith('|---'):
                            usage_table_end = i
                            break
                    
                    if usage_table_end is not None:
                        print(f"✓ Report contains usage statistics with data rows")
                    else:
                        print(f"✗ Report has usage statistics header but missing data rows")
                        success = False
                else:
                    print(f"✗ Report missing usage statistics table")
                    success = False
            else:
                print(f"✗ Report missing usage statistics section")
                success = False
            
            if '## Chat-Usage Correlation Report' in content:
                if '| Date | Chats | Matched | Unmatched | Usage Req |' in content:
                    print(f"✓ Report contains correlation report with daily statistics")
                else:
                    print(f"✗ Report has correlation report header but missing daily statistics table")
                    success = False
            else:
                print(f"✗ Report missing correlation report section")
                success = False
            
            if '## Task Clustering Report' in content:
                if '### Clustering Statistics' in content:
                    print(f"✓ Report contains clustering report with statistics")
                else:
                    print(f"✗ Report has clustering report header but missing statistics section")
                    success = False
            else:
                print(f"✗ Report missing clustering report section")
                success = False
            
            has_specs_section = False
            if '## Technical requirements' in content or '## Domain requirements' in content:
                has_specs_section = True
                print(f"✓ Report contains specifications section")
            else:
                task_summaries_idx = content.find('# Task Summaries')
                if task_summaries_idx > 0:
                    before_summaries = content[:task_summaries_idx]
                    if '## Technical requirements' in before_summaries or '## Domain requirements' in before_summaries:
                        has_specs_section = True
                        print(f"✓ Report contains specifications section before task summaries")
                
                if not has_specs_section:
                    print(f"  ERROR: Specifications section not found (may be empty or not generated)")
                    success = False
    
    print()
    return success


def main():
    check_env_vars()
    
    md_file = 'EXAMPLE.md'
    csv_file = 'EXAMPLE.csv'
    
    if not Path(md_file).exists():
        print(f"Error: {md_file} not found")
        sys.exit(1)
    
    if not Path(csv_file).exists():
        print(f"Error: {csv_file} not found")
        sys.exit(1)
    
    success = run_pipeline(md_file, csv_file)
    
    if success:
        print("=" * 80)
        print("All tests passed!")
        print("=" * 80)
        sys.exit(0)
    else:
        print("=" * 80)
        print("Tests failed!")
        print("=" * 80)
        sys.exit(1)


if __name__ == '__main__':
    main()
