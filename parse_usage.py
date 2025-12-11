#!/usr/bin/env python3
import sqlite3
import argparse
import csv
from datetime import datetime
from typing import Dict


class UsageParser:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._create_table()
    
    def _create_table(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage (
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
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON usage(timestamp)")
        self.conn.commit()
    
    def has_usage_data(self) -> bool:
        """Check if usage data already exists in database."""
        count = self.cursor.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
        return count > 0
    
    def load_csv(self, csv_file: str, skip_if_exists: bool = True):
        if skip_if_exists and self.has_usage_data():
            max_timestamp = self.cursor.execute("SELECT MAX(timestamp) FROM usage").fetchone()[0]
            if max_timestamp:
                print(f"Existing usage data found (latest timestamp: {datetime.fromtimestamp(max_timestamp).isoformat()})")
                print("Will only add new records with timestamps after this point")
            else:
                count = self.cursor.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
                print(f"Skipping usage parsing: {count} usage records already exist in database")
                return False
        else:
            max_timestamp = None
            self.cursor.execute("DELETE FROM usage")
            self.conn.commit()
        
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = []
            skipped_count = 0
            for row in reader:
                date_str = row['Date'].strip('"')
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    timestamp = dt.timestamp()
                except:
                    continue
                
                if max_timestamp is not None and timestamp <= max_timestamp:
                    skipped_count += 1
                    continue
                
                existing = self.cursor.execute("""
                    SELECT id FROM usage WHERE timestamp = ? AND date = ?
                """, (timestamp, date_str)).fetchone()
                if existing:
                    skipped_count += 1
                    continue
                kind = row.get('Kind', '').strip('"')
                if 'Error' in kind:
                    continue
                rows.append((
                    date_str,
                    kind,
                    row.get('Model', '').strip('"'),
                    row.get('Max Mode', '').strip('"'),
                    int(row.get('Input (w/ Cache Write)', '0') or '0'),
                    int(row.get('Input (w/o Cache Write)', '0') or '0'),
                    int(row.get('Cache Read', '0') or '0'),
                    int(row.get('Output Tokens', '0') or '0'),
                    int(row.get('Total Tokens', '0') or '0'),
                    float(row.get('Cost', '0') or '0'),
                    timestamp
                ))
        
        if skipped_count > 0:
            print(f"Skipped {skipped_count} existing records")
        
        if rows:
            self.cursor.executemany("""
                INSERT INTO usage (
                    date, kind, model, max_mode,
                    input_with_cache_write, input_without_cache_write, cache_read,
                    output_tokens, total_tokens, cost, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            self.conn.commit()
            print(f"Added {len(rows)} new usage records")
            return True
        else:
            print("No new records to add")
            return False
    
    def get_overall_stats(self) -> Dict:
        stats = self.cursor.execute("""
            SELECT 
                COUNT(*) as total_requests,
                SUM(input_with_cache_write) as total_input_w_cache,
                SUM(input_without_cache_write) as total_input_wo_cache,
                SUM(cache_read) as total_cache_read,
                SUM(output_tokens) as total_output,
                SUM(total_tokens) as total_tokens,
                AVG(input_with_cache_write) as avg_input_w_cache,
                AVG(input_without_cache_write) as avg_input_wo_cache,
                AVG(cache_read) as avg_cache_read,
                AVG(output_tokens) as avg_output,
                AVG(total_tokens) as avg_total
            FROM usage
        """).fetchone()
        
        return {
            'total_requests': stats[0] or 0,
            'total_input_w_cache': stats[1] or 0,
            'total_input_wo_cache': stats[2] or 0,
            'total_cache_read': stats[3] or 0,
            'total_output': stats[4] or 0,
            'total_tokens': stats[5] or 0,
            'avg_input_w_cache': stats[6] or 0.0,
            'avg_input_wo_cache': stats[7] or 0.0,
            'avg_cache_read': stats[8] or 0.0,
            'avg_output': stats[9] or 0.0,
            'avg_total': stats[10] or 0.0,
        }
    
    def calculate_tps_stats(self, window_minutes: int) -> Dict:
        window_seconds = window_minutes * 60
        
        rows = self.cursor.execute("""
            SELECT 
                id, timestamp,
                input_with_cache_write, input_without_cache_write,
                cache_read, output_tokens, total_tokens
            FROM usage
            ORDER BY timestamp
        """).fetchall()
        
        token_totals = {
            'input_w_cache': 0,
            'input_wo_cache': 0,
            'cache_read': 0,
            'output': 0,
            'total': 0,
        }
        individual_tps_sums = {
            'input_w_cache': 0.0,
            'input_wo_cache': 0.0,
            'cache_read': 0.0,
            'output': 0.0,
            'total': 0.0,
        }
        total_time = 0.0
        request_count = 0
        
        for i in range(1, len(rows)):
            prev_row = rows[i-1]
            curr_row = rows[i]
            
            time_diff = curr_row[1] - prev_row[1]
            
            if 0 < time_diff <= window_seconds:
                request_count += 1
                total_time += time_diff
                
                token_totals['input_w_cache'] += curr_row[2]
                token_totals['input_wo_cache'] += curr_row[3]
                token_totals['cache_read'] += curr_row[4]
                token_totals['output'] += curr_row[5]
                token_totals['total'] += curr_row[6]
                
                if time_diff > 0:
                    individual_tps_sums['input_w_cache'] += curr_row[2] / time_diff
                    individual_tps_sums['input_wo_cache'] += curr_row[3] / time_diff
                    individual_tps_sums['cache_read'] += curr_row[4] / time_diff
                    individual_tps_sums['output'] += curr_row[5] / time_diff
                    individual_tps_sums['total'] += curr_row[6] / time_diff
        
        overall_tps = {}
        avg_tps = {}
        for key in token_totals:
            overall_tps[key] = token_totals[key] / total_time if total_time > 0 else 0.0
            avg_tps[key] = individual_tps_sums[key] / request_count if request_count > 0 else 0.0
        
        return {
            'request_count': request_count,
            'token_totals': token_totals,
            'total_time': total_time,
            'overall_tps': overall_tps,
            'avg_tps': avg_tps
        }
    
    def print_unified_stats(self):
        stats = self.get_overall_stats()
        
        tps_by_window = {}
        for window in [1, 2, 3, 4, 5, 6, 7]:
            tps_by_window[window] = self.calculate_tps_stats(window)
        
        print("## Usage Statistics\n")
        print("TPS metrics shown only for consecutive requests within the specified time window.\n")
        
        tps_columns = [
            ('Input (w/ Cache Write)', 'input_w_cache', 'total_input_w_cache', 'avg_input_w_cache'),
            ('Input (w/o Cache Write)', 'input_wo_cache', 'total_input_wo_cache', 'avg_input_wo_cache'),
            ('Cache Read', 'cache_read', 'total_cache_read', 'avg_cache_read'),
            ('Output Tokens', 'output', 'total_output', 'avg_output'),
            ('Total Tokens', 'total', 'total_tokens', 'avg_total'),
        ]
        
        header_parts = ["Metric", "Requests"]
        for metric_name, _, _, _ in tps_columns:
            header_parts.append(metric_name)
        
        header = "| " + " | ".join(header_parts) + " |"
        print(header)
        
        separator_parts = [("-" * len(part)) for part in header_parts]
        separator = "|" + "|".join([" " + sep + " " for sep in separator_parts]) + "|"
        print(separator)
        
        row = f"| Total | {stats['total_requests']:,} |"
        for _, _, total_key, _ in tps_columns:
            row += f" {stats[total_key]:,} |"
        print(row)
        
        row = "| Avg/Req | - |"
        for _, _, _, avg_key in tps_columns:
            row += f" {stats[avg_key]:,.2f} |"
        print(row)
        
        for calc_type, calc_key in [('Avg TPS', 'avg_tps'), ('Overall TPS', 'overall_tps')]:
            for window in [1, 2, 3, 4, 5, 6, 7]:
                window_data = tps_by_window[window]
                row = f"| {window}min {calc_type} | {window_data['request_count']:,} |"
                for _, tps_key, _, _ in tps_columns:
                    tps_val = window_data[calc_key][tps_key]
                    row += f" {tps_val:,.2f} |"
                print(row)
        print()
    
    def close(self):
        self.conn.close()


def main():
    parser = argparse.ArgumentParser(description='Parse usage CSV file into SQLite database and show statistics')
    parser.add_argument('csv_file', help='Path to CSV file to parse')
    parser.add_argument('--db-file', default=None, help='SQLite database path (default: same as csv_file with .db extension)')
    parser.add_argument('--force', action='store_true', help='Force re-parsing even if usage data exists')
    
    args = parser.parse_args()
    
    from db_utils import derive_db_path_from_file
    db_path = derive_db_path_from_file(args.csv_file, args.db_file)
    
    parser_obj = UsageParser(db_path)
    try:
        parsed = parser_obj.load_csv(args.csv_file, skip_if_exists=not args.force)
        parser_obj.print_unified_stats()
    finally:
        parser_obj.close()


if __name__ == '__main__':
    main()

