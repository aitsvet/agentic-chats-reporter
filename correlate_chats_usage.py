#!/usr/bin/env python3
import sqlite3
import argparse
from typing import List, Dict, Tuple
import glob
from task_builder import TaskBuilder
from db_utils import find_db_file, add_db_file_argument


class ChatUsageCorrelator:
    def __init__(self, chats_db: str, usage_db_pattern: str):
        self.chats_conn = sqlite3.connect(chats_db)
        self.chats_cursor = self.chats_conn.cursor()
        
        usage_dbs = glob.glob(usage_db_pattern)
        if not usage_dbs:
            raise ValueError(f"No usage databases found matching pattern: {usage_db_pattern}")
        
        self.usage_conn = sqlite3.connect(usage_dbs[0])
        self.usage_cursor = self.usage_conn.cursor()
        
        if len(usage_dbs) > 1:
            for db_path in usage_dbs[1:]:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("ATTACH DATABASE ? AS other", (db_path,))
                self.usage_cursor.execute("""
                    INSERT INTO usage SELECT * FROM other.usage
                """)
                conn.close()
            self.usage_conn.commit()
        
        self.usage_cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON usage(timestamp)")
        self.usage_conn.commit()
        
        self.task_builder = TaskBuilder(chats_db)
    
    def get_message_tasks(self) -> List[Dict]:
        return self.task_builder.get_message_tasks()
    
    def correlate_tasks_with_usage(self, tasks: List[Dict], strict_window: float = 600, relaxed_window: float = 7200) -> Tuple[List[Dict], List[Dict], Dict]:
        all_usage_requests = self.usage_cursor.execute("""
            SELECT id, timestamp, input_with_cache_write, input_without_cache_write,
                   cache_read, output_tokens, total_tokens, kind, model
            FROM usage
            ORDER BY timestamp
        """).fetchall()
        
        usage_request_map = {req[0]: req for req in all_usage_requests}
        usage_request_claimed_by = {} 
        task_matches = {}
        
        tasks_sorted = sorted(tasks, key=lambda s: s['user_timestamp'])
        
        min_usage_ts = min(r[1] for r in all_usage_requests) if all_usage_requests else 0
        max_usage_ts = max(r[1] for r in all_usage_requests) if all_usage_requests else 0
        
        for pass_num, (window, max_shares_close, max_shares_far) in enumerate([
            (strict_window, 10, 30),
            (relaxed_window, 300, 500)
        ]):
            for task in tasks_sorted:
                if task['user_msg_id'] in task_matches:
                    continue
                    
                user_ts = task['user_timestamp']
                task_end_ts = task['task_end_timestamp']
                
                if user_ts < min_usage_ts - 172800:
                    continue
                
                window_start = user_ts - window
                window_end = max(task_end_ts, user_ts) + window
                
                candidate_matches = []
                
                for req_id, req_data in usage_request_map.items():
                    req_ts = req_data[1]
                    
                    if req_ts < window_start or req_ts > window_end:
                        continue
                    
                    time_diff = abs(req_ts - user_ts)
                    
                    if time_diff <= window:
                        candidate_matches.append((req_id, req_data, time_diff))
                
                if candidate_matches:
                    candidate_matches.sort(key=lambda x: x[2])
                    
                    matches = []
                    
                    for req_id, req_data, time_diff in candidate_matches:
                        existing_claims = usage_request_claimed_by.get(req_id, set())
                        
                        max_shares = max_shares_close if time_diff <= strict_window else max_shares_far
                        
                        if len(existing_claims) < max_shares:
                            matches.append((req_id, req_data, time_diff))
                            if req_id not in usage_request_claimed_by:
                                usage_request_claimed_by[req_id] = set()
                            usage_request_claimed_by[req_id].add(task['user_msg_id'])
                    
                    if matches:
                        task_matches[task['user_msg_id']] = matches
        
        for req_id, req_data in usage_request_map.items():
            if req_id in usage_request_claimed_by:
                continue
            
            req_ts = req_data[1]
            
            best_match = None
            best_time_diff = float('inf')
            
            for task in tasks_sorted:
                user_ts = task['user_timestamp']
                
                if user_ts < min_usage_ts - 172800:
                    continue
                
                time_diff = abs(req_ts - user_ts)
                
                if time_diff <= relaxed_window and time_diff < best_time_diff:
                    best_match = task
                    best_time_diff = time_diff
            
            if best_match and best_match['user_msg_id'] in task_matches:
                if req_id not in usage_request_claimed_by:
                    usage_request_claimed_by[req_id] = set()
                usage_request_claimed_by[req_id].add(best_match['user_msg_id'])
                match_list = task_matches[best_match['user_msg_id']]
                match_list.append((req_id, req_data, best_time_diff))
        
        correlated = []
        unmatched_tasks = []
        
        for task in tasks:
            if task['user_msg_id'] not in task_matches:
                unmatched_tasks.append(task)
                continue
            
            match_list = task_matches[task['user_msg_id']]
            usage_requests = [m[1] for m in match_list]
            
            total_input_w_cache = sum(r[2] for r in usage_requests)
            total_input_wo_cache = sum(r[3] for r in usage_requests)
            total_cache_read = sum(r[4] for r in usage_requests)
            total_output = sum(r[5] for r in usage_requests)
            total_tokens = sum(r[6] for r in usage_requests)
            
            correlated.append({
                **task,
                'matched_usage_count': len(usage_requests),
                'matched_usage_ids': [m[0] for m in match_list],
                'matched_input_w_cache': total_input_w_cache,
                'matched_input_wo_cache': total_input_wo_cache,
                'matched_cache_read': total_cache_read,
                'matched_output': total_output,
                'matched_total_tokens': total_tokens,
                'first_usage_timestamp': usage_requests[0][1] if usage_requests else None,
                'last_usage_timestamp': usage_requests[-1][1] if usage_requests else None,
                'time_diff': match_list[0][2] if match_list else None
            })
        
        all_matched_usage_ids = set(usage_request_claimed_by.keys())
        
        if all_matched_usage_ids:
            placeholders = ','.join(['?'] * len(all_matched_usage_ids))
            query = f"""
                SELECT id, timestamp, input_with_cache_write, input_without_cache_write,
                       cache_read, output_tokens, total_tokens, kind, model, date
                FROM usage
                WHERE id NOT IN ({placeholders})
                ORDER BY timestamp
            """
            unmatched_usage = self.usage_cursor.execute(query, list(all_matched_usage_ids)).fetchall()
        else:
            unmatched_usage = self.usage_cursor.execute("""
                SELECT id, timestamp, input_with_cache_write, input_without_cache_write,
                       cache_read, output_tokens, total_tokens, kind, model, date
                FROM usage
                ORDER BY timestamp
            """).fetchall()
        
        stats = {
            'total_tasks': len(tasks),
            'matched_tasks': len(correlated),
            'unmatched_tasks': len(unmatched_tasks),
            'matched_usage_requests': len(all_matched_usage_ids),
            'unmatched_usage_requests': len(unmatched_usage)
        }
        
        return correlated, unmatched_tasks, unmatched_usage, stats
    
    def calculate_correlations(self, correlated: List[Dict]) -> Dict:
        if not correlated:
            return {}
        
        content_lengths = [s['total_content_length'] for s in correlated]
        user_content_lengths = [s['user_content_length'] for s in correlated]
        agent_content_lengths = [s['agent_total_length'] for s in correlated]
        agent_text_lengths = [s['agent_text_length'] for s in correlated]
        token_totals = [s['matched_total_tokens'] for s in correlated]
        input_tokens_with_cache = [s['matched_input_w_cache'] + s['matched_input_wo_cache'] + s['matched_cache_read'] for s in correlated]
        input_tokens_without_cache = [s['matched_input_w_cache'] + s['matched_input_wo_cache'] for s in correlated]
        output_tokens = [s['matched_output'] for s in correlated]
        tokens_without_cache = [s['matched_input_w_cache'] + s['matched_input_wo_cache'] + s['matched_output'] for s in correlated]
        
        def correlation(x, y):
            if len(x) != len(y) or len(x) == 0:
                return None
            n = len(x)
            mean_x = sum(x) / n
            mean_y = sum(y) / n
            
            numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
            denom_x = sum((x[i] - mean_x) ** 2 for i in range(n))
            denom_y = sum((y[i] - mean_y) ** 2 for i in range(n))
            
            if denom_x == 0 or denom_y == 0:
                return None
            
            return numerator / (denom_x ** 0.5 * denom_y ** 0.5)
        
        return {
            'content_vs_total_tokens': correlation(content_lengths, token_totals),
            'content_vs_input_tokens': correlation(content_lengths, input_tokens_with_cache),
            'content_vs_input_tokens_no_cache': correlation(content_lengths, input_tokens_without_cache),
            'content_vs_output_tokens': correlation(content_lengths, output_tokens),
            'agent_content_vs_output_tokens': correlation(agent_content_lengths, output_tokens),
            'agent_text_vs_output_tokens': correlation(agent_text_lengths, output_tokens),
            'content_vs_tokens_no_cache': correlation(content_lengths, tokens_without_cache),
            'avg_content_length': sum(content_lengths) / len(content_lengths) if content_lengths else 0,
            'avg_user_content_length': sum(user_content_lengths) / len(user_content_lengths) if user_content_lengths else 0,
            'avg_agent_content_length': sum(agent_content_lengths) / len(agent_content_lengths) if agent_content_lengths else 0,
            'avg_agent_text_length': sum(agent_text_lengths) / len(agent_text_lengths) if agent_text_lengths else 0,
            'avg_total_tokens': sum(token_totals) / len(token_totals) if token_totals else 0,
            'avg_input_tokens': sum(input_tokens_with_cache) / len(input_tokens_with_cache) if input_tokens_with_cache else 0,
            'avg_input_tokens_no_cache': sum(input_tokens_without_cache) / len(input_tokens_without_cache) if input_tokens_without_cache else 0,
            'avg_output_tokens': sum(output_tokens) / len(output_tokens) if output_tokens else 0,
            'avg_tokens_no_cache': sum(tokens_without_cache) / len(tokens_without_cache) if tokens_without_cache else 0,
            'total_content': sum(content_lengths),
            'total_user_content': sum(user_content_lengths),
            'total_agent_content': sum(agent_content_lengths),
            'total_agent_text': sum(agent_text_lengths),
            'total_matched_tokens': sum(token_totals),
            'total_matched_input': sum(input_tokens_with_cache),
            'total_matched_input_no_cache': sum(input_tokens_without_cache),
            'total_matched_output': sum(output_tokens),
            'total_matched_tokens_no_cache': sum(tokens_without_cache)
        }
    
    def calculate_daily_stats(self, correlated: List[Dict], unmatched_tasks: List[Dict],
                              unmatched_usage: List[Tuple], all_tasks: List[Dict]) -> Dict:
        from datetime import datetime, timezone
        from collections import defaultdict
        
        daily_stats = defaultdict(lambda: {
            'tasks': 0,
            'matched_tasks': 0,
            'unmatched_tasks': 0,
            'usage_requests': 0,
            'matched_usage_requests': 0,
            'unmatched_usage_requests': 0,
            'total_content_length': 0,
            'matched_content_length': 0,
            'total_tokens': 0,
            'matched_tokens': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'matched_input_tokens': 0,
            'matched_output_tokens': 0
        })
        
        all_matched_task_ids = {s['user_msg_id'] for s in correlated}
        all_matched_usage_ids = set()
        for task in correlated:
            all_matched_usage_ids.update(task['matched_usage_ids'])
        
        for task in all_tasks:
            user_ts = task['user_timestamp']
            day_key = datetime.fromtimestamp(user_ts, tz=timezone.utc).strftime('%Y-%m-%d')
            daily_stats[day_key]['tasks'] += 1
            daily_stats[day_key]['total_content_length'] += task['total_content_length']
            
            if task['user_msg_id'] in all_matched_task_ids:
                daily_stats[day_key]['matched_tasks'] += 1
                daily_stats[day_key]['matched_content_length'] += task['total_content_length']
            else:
                daily_stats[day_key]['unmatched_tasks'] += 1
        
        all_usage_requests = self.usage_cursor.execute("""
            SELECT id, timestamp, input_with_cache_write, input_without_cache_write,
                   cache_read, output_tokens, total_tokens
            FROM usage
        """).fetchall()
        
        for req_id, req_ts, in_w, in_wo, cache, out, total in all_usage_requests:
            day_key = datetime.fromtimestamp(req_ts, tz=timezone.utc).strftime('%Y-%m-%d')
            daily_stats[day_key]['usage_requests'] += 1
            daily_stats[day_key]['total_tokens'] += total
            daily_stats[day_key]['input_tokens'] += (in_w + in_wo + cache)
            daily_stats[day_key]['output_tokens'] += out
            
            if req_id in all_matched_usage_ids:
                daily_stats[day_key]['matched_usage_requests'] += 1
                daily_stats[day_key]['matched_tokens'] += total
                daily_stats[day_key]['matched_input_tokens'] += (in_w + in_wo + cache)
                daily_stats[day_key]['matched_output_tokens'] += out
            else:
                daily_stats[day_key]['unmatched_usage_requests'] += 1
        
        return dict(sorted(daily_stats.items()))
    
    def print_report(self, correlated: List[Dict], unmatched_tasks: List[Dict], 
                     unmatched_usage: List[Tuple], stats: Dict, correlations: Dict, daily_stats: Dict):
        print("## Chat-Usage Correlation Report\n")
        
        print("### Daily Statistics\n")
        print("| Date | Chats | Matched | Unmatched | Usage Req | Matched | Unmatched | Content (KB) | Matched Content (KB) | Total Tokens (M) | Matched Tokens (M) | Input Tokens (M) | Output Tokens (K) |")
        print("|------|-------|---------|-----------|-----------|---------|-----------|--------------|---------------------|------------------|-------------------|------------------|-------------------|")
        
        total_stats = {
            'tasks': 0,
            'matched_tasks': 0,
            'unmatched_tasks': 0,
            'usage_requests': 0,
            'matched_usage_requests': 0,
            'unmatched_usage_requests': 0,
            'total_content_length': 0,
            'matched_content_length': 0,
            'total_tokens': 0,
            'matched_tokens': 0,
            'input_tokens': 0,
            'output_tokens': 0
        }
        
        for day, day_data in daily_stats.items():
            chats = day_data['tasks']
            matched_chats = day_data['matched_tasks']
            unmatched_chats = day_data['unmatched_tasks']
            usage_req = day_data['usage_requests']
            matched_usage = day_data['matched_usage_requests']
            unmatched_usage_count = day_data['unmatched_usage_requests']
            content_kb = day_data['total_content_length'] / 1024
            matched_content_kb = day_data['matched_content_length'] / 1024
            total_tokens_m = day_data['total_tokens'] / 1_000_000
            matched_tokens_m = day_data['matched_tokens'] / 1_000_000
            input_tokens_m = day_data['input_tokens'] / 1_000_000
            output_tokens_k = day_data['output_tokens'] / 1_000
            
            print(f"| {day} | {chats} | {matched_chats} | {unmatched_chats} | {usage_req} | {matched_usage} | {unmatched_usage_count} | {content_kb:.0f} | {matched_content_kb:.0f} | {total_tokens_m:.2f} | {matched_tokens_m:.2f} | {input_tokens_m:.2f} | {output_tokens_k:.0f} |")
            
            total_stats['tasks'] += chats
            total_stats['matched_tasks'] += matched_chats
            total_stats['unmatched_tasks'] += unmatched_chats
            total_stats['usage_requests'] += usage_req
            total_stats['matched_usage_requests'] += matched_usage
            total_stats['unmatched_usage_requests'] += unmatched_usage_count
            total_stats['total_content_length'] += day_data['total_content_length']
            total_stats['matched_content_length'] += day_data['matched_content_length']
            total_stats['total_tokens'] += day_data['total_tokens']
            total_stats['matched_tokens'] += day_data['matched_tokens']
            total_stats['input_tokens'] += day_data['input_tokens']
            total_stats['output_tokens'] += day_data['output_tokens']
        
        total_content_kb = total_stats['total_content_length'] / 1024
        total_matched_content_kb = total_stats['matched_content_length'] / 1024
        total_tokens_m = total_stats['total_tokens'] / 1_000_000
        total_matched_tokens_m = total_stats['matched_tokens'] / 1_000_000
        total_input_tokens_m = total_stats['input_tokens'] / 1_000_000
        total_output_tokens_k = total_stats['output_tokens'] / 1_000
        
        total_usage_req = total_stats['usage_requests']
        total_matched_req = total_stats['matched_usage_requests']
        total_unmatched_req = total_stats['unmatched_usage_requests']
        
        matched_req_pct = (total_matched_req / total_usage_req * 100) if total_usage_req > 0 else 0
        unmatched_req_pct = (total_unmatched_req / total_usage_req * 100) if total_usage_req > 0 else 0
        
        all_total_tokens = total_stats['total_tokens']
        all_matched_tokens = total_stats['matched_tokens']
        all_unmatched_tokens = all_total_tokens - all_matched_tokens
        matched_tokens_pct = (all_matched_tokens / all_total_tokens * 100) if all_total_tokens > 0 else 0
        unmatched_tokens_pct = (all_unmatched_tokens / all_total_tokens * 100) if all_total_tokens > 0 else 0
        
        print(f"| **Total** | **{total_stats['tasks']}** | **{total_stats['matched_tasks']}** | **{total_stats['unmatched_tasks']}** | **{total_usage_req}** | **{total_matched_req} ({matched_req_pct:.1f}%)** | **{total_unmatched_req} ({unmatched_req_pct:.1f}%)** | **{total_content_kb:.0f}** | **{total_matched_content_kb:.0f}** | **{total_tokens_m:.2f}** | **{total_matched_tokens_m:.2f} ({matched_tokens_pct:.1f}%)** | **{total_input_tokens_m:.2f}** | **{total_output_tokens_k:.0f}** |")
        print()
        
        if correlations:
            print("### Content Size vs Token Count Correlations\n")
            
            corr_no_cache = correlations.get('content_vs_tokens_no_cache')
            corr_input_no_cache = correlations.get('content_vs_input_tokens_no_cache')
            corr_output = correlations.get('content_vs_output_tokens')
            corr_agent_output = correlations.get('agent_content_vs_output_tokens')
            corr_agent_text_output = correlations.get('agent_text_vs_output_tokens')
            corr_total = correlations.get('content_vs_total_tokens')
            
            avg_content = correlations.get('avg_content_length', 0)
            avg_user_content = correlations.get('avg_user_content_length', 0)
            avg_agent_content = correlations.get('avg_agent_content_length', 0)
            avg_agent_text = correlations.get('avg_agent_text_length', 0)
            avg_tokens_no_cache = correlations.get('avg_tokens_no_cache', 0)
            avg_input_no_cache = correlations.get('avg_input_tokens_no_cache', 0)
            avg_output = correlations.get('avg_output_tokens', 0)
            avg_total = correlations.get('avg_total_tokens', 0)
            
            total_content = correlations.get('total_content', 0)
            total_user_content = correlations.get('total_user_content', 0)
            total_agent_content = correlations.get('total_agent_content', 0)
            total_agent_text = correlations.get('total_agent_text', 0)
            total_tokens_no_cache = correlations.get('total_matched_tokens_no_cache', 0)
            total_input_no_cache = correlations.get('total_matched_input_no_cache', 0)
            total_output = correlations.get('total_matched_output', 0)
            total_tokens = correlations.get('total_matched_tokens', 0)
            
            chars_per_token_output_agent_text = (avg_agent_text / avg_output) if avg_output > 0 and avg_agent_text > 0 else 0
            chars_per_token_output_agent_total = (avg_agent_content / avg_output) if avg_output > 0 and avg_agent_content > 0 else 0
            chars_per_token_output_total = (avg_content / avg_output) if avg_output > 0 else 0
            chars_per_token_no_cache = (avg_content / avg_tokens_no_cache) if avg_tokens_no_cache > 0 else 0
            
            print("| Type | Correlation | Average | Total |")
            print("|------|-------------|---------|-------|")
            
            corr_str = f"{corr_no_cache:.4f}" if corr_no_cache is not None else "N/A"
            avg_str = f"{avg_tokens_no_cache:,.0f}" if avg_tokens_no_cache > 0 else "0"
            total_str = f"{total_tokens_no_cache:,}" if total_tokens_no_cache > 0 else "0"
            print(f"| Tokens (excluding cache) | {corr_str} | {avg_str} tokens | {total_str} tokens |")
            
            corr_str = f"{corr_input_no_cache:.4f}" if corr_input_no_cache is not None else "N/A"
            avg_str = f"{avg_input_no_cache:,.0f}" if avg_input_no_cache > 0 else "0"
            total_str = f"{total_input_no_cache:,}" if total_input_no_cache > 0 else "0"
            print(f"| Input Tokens (excluding cache) | {corr_str} | {avg_str} tokens | {total_str} tokens |")
            
            corr_str = f"{corr_output:.4f}" if corr_output is not None else "N/A"
            avg_str = f"{avg_output:,.0f}" if avg_output > 0 else "0"
            total_str = f"{total_output:,}" if total_output > 0 else "0"
            print(f"| Output Tokens | {corr_str} | {avg_str} tokens | {total_str} tokens |")
            
            corr_str = f"{corr_agent_output:.4f}" if corr_agent_output is not None else "N/A"
            print(f"| Agent Content (all) vs Output Tokens | {corr_str} | - | - |")
            
            corr_str = f"{corr_agent_text_output:.4f}" if corr_agent_text_output is not None else "N/A"
            print(f"| Agent Text (no tool calls) vs Output Tokens | {corr_str} | - | - |")
            
            corr_str = f"{corr_total:.4f}" if corr_total is not None else "N/A"
            avg_str = f"{avg_total:,.0f}" if avg_total > 0 else "0"
            total_str = f"{total_tokens:,}" if total_tokens > 0 else "0"
            print(f"| Total Tokens (with cache) | {corr_str} | {avg_str} tokens | {total_str} tokens |")
            
            avg_content_str = f"{avg_content:,.0f}" if avg_content > 0 else "0"
            total_content_str = f"{total_content:,}" if total_content > 0 else "0"
            print(f"| Total Content Length (user+agent) | - | {avg_content_str} characters | {total_content_str} characters |")
            
            avg_agent_content_str = f"{avg_agent_content:,.0f}" if avg_agent_content > 0 else "0"
            total_agent_content_str = f"{total_agent_content:,}" if total_agent_content > 0 else "0"
            print(f"| Agent Content Length (all) | - | {avg_agent_content_str} characters | {total_agent_content_str} characters |")
            
            avg_agent_text_str = f"{avg_agent_text:,.0f}" if avg_agent_text > 0 else "0"
            total_agent_text_str = f"{total_agent_text:,}" if total_agent_text > 0 else "0"
            print(f"| Agent Text Length (no tool calls) | - | {avg_agent_text_str} characters | {total_agent_text_str} characters |")
            
            if avg_output > 0 and avg_agent_text > 0 and chars_per_token_output_agent_text > 0:
                chars_per_token_str = f"{chars_per_token_output_agent_text:.2f}"
                print(f"| Characters per Token (agent text vs output) | - | {chars_per_token_str} | - |")
            
            if avg_output > 0 and avg_agent_content > 0 and chars_per_token_output_agent_total > 0:
                chars_per_token_str = f"{chars_per_token_output_agent_total:.2f}"
                print(f"| Characters per Token (agent all vs output) | - | {chars_per_token_str} | - |")
            
            if avg_output > 0 and chars_per_token_output_total > 0:
                chars_per_token_str = f"{chars_per_token_output_total:.2f}"
                print(f"| Characters per Token (total content vs output) | - | {chars_per_token_str} | - |")
            
            print()
            print("**Correlation values** are Pearson correlation coefficients (ranging from -1 to +1) measuring the linear relationship between content size and token counts. ")
            print("A value close to +1 indicates a strong positive correlation (larger content = more tokens), ")
            print("while a value close to -1 indicates a strong negative correlation (larger content = fewer tokens). ")
            print("Values near 0 indicate weak or no linear relationship. ")
            print("Negative correlations may occur when token counts are influenced by factors other than content size, ")
            print("such as caching effects, tokenization differences, or the matching algorithm pairing tasks with usage requests from different contexts.\n")
        
        if unmatched_usage or unmatched_tasks:
            all_usage_total_tokens = self.usage_cursor.execute("SELECT SUM(total_tokens) FROM usage").fetchone()[0] or 0
            all_usage_total_input = self.usage_cursor.execute("SELECT SUM(input_with_cache_write + input_without_cache_write + cache_read) FROM usage").fetchone()[0] or 0
            all_usage_total_output = self.usage_cursor.execute("SELECT SUM(output_tokens) FROM usage").fetchone()[0] or 0
            all_usage_count = self.usage_cursor.execute("SELECT COUNT(*) FROM usage").fetchone()[0] or 0
            
            total_tasks = stats.get('total_tasks', 0)
            matched_tasks = stats.get('matched_tasks', 0)
            unmatched_tasks_count = len(unmatched_tasks) if unmatched_tasks else 0
            
            print("### Unmatched Data Summary\n")
            
            if unmatched_usage:
                print(f"Found {len(unmatched_usage)} usage requests that don't match any chat tasks (likely represent chats missing from the log).\n")
            
            if unmatched_tasks:
                print(f"Found {unmatched_tasks_count} message tasks that don't match any usage requests (may represent chats that occurred outside the usage data time window).\n")
            
            total_unmatched_tokens = sum(r[6] for r in unmatched_usage) if unmatched_usage else 0
            total_unmatched_input = sum(r[2] + r[3] + r[4] for r in unmatched_usage) if unmatched_usage else 0
            total_unmatched_output = sum(r[5] for r in unmatched_usage) if unmatched_usage else 0
            
            unmatched_req_pct = (len(unmatched_usage) / all_usage_count * 100) if all_usage_count > 0 and unmatched_usage else 0
            unmatched_tokens_pct = (total_unmatched_tokens / all_usage_total_tokens * 100) if all_usage_total_tokens > 0 and unmatched_usage else 0
            unmatched_input_pct = (total_unmatched_input / all_usage_total_input * 100) if all_usage_total_input > 0 and unmatched_usage else 0
            unmatched_output_pct = (total_unmatched_output / all_usage_total_output * 100) if all_usage_total_output > 0 and unmatched_usage else 0
            
            unmatched_task_pct = (unmatched_tasks_count / total_tasks * 100) if total_tasks > 0 and unmatched_tasks else 0
            matched_task_pct = (matched_tasks / total_tasks * 100) if total_tasks > 0 else 0
            
            total_unmatched_content = sum(s['total_content_length'] for s in unmatched_tasks) if unmatched_tasks else 0
            total_content_all = total_stats.get('total_content_length', 0)
            unmatched_content_pct = (total_unmatched_content / total_content_all * 100) if total_content_all > 0 and unmatched_tasks else 0
            
            print("| Category | Metric | Count/Value | Percentage |")
            print("|----------|--------|-------------|------------|")
            
            if unmatched_usage:
                print(f"| Usage Requests | Unmatched Requests | {len(unmatched_usage):,} | {unmatched_req_pct:.1f}% |")
                print(f"| Usage Requests | Total Requests | {all_usage_count:,} | 100.0% |")
                print(f"| Usage Requests | Unmatched Total Tokens | {total_unmatched_tokens:,} | {unmatched_tokens_pct:.1f}% |")
                print(f"| Usage Requests | Unmatched Input Tokens | {total_unmatched_input:,} | {unmatched_input_pct:.1f}% |")
                print(f"| Usage Requests | Unmatched Output Tokens | {total_unmatched_output:,} | {unmatched_output_pct:.1f}% |")
            
            if unmatched_tasks:
                print(f"| Message Tasks | Unmatched Tasks | {unmatched_tasks_count} | {unmatched_task_pct:.1f}% |")
                print(f"| Message Tasks | Matched Tasks | {matched_tasks} | {matched_task_pct:.1f}% |")
                print(f"| Message Tasks | Total Tasks | {total_tasks} | 100.0% |")
                print(f"| Message Tasks | Unmatched Content | {total_unmatched_content:,} characters | {unmatched_content_pct:.1f}% |")
            
            print()
    
    def run(self):
        print("Extracting message tasks...")
        tasks = self.get_message_tasks()
        print(f"Found {len(tasks)} user message tasks\n")
        
        print("Correlating with usage requests...")
        correlated, unmatched_tasks, unmatched_usage, stats = self.correlate_tasks_with_usage(tasks, strict_window=600, relaxed_window=7200)
        
        print("Calculating correlations...")
        correlations = self.calculate_correlations(correlated)
        
        print("Calculating daily statistics...")
        daily_stats = self.calculate_daily_stats(correlated, unmatched_tasks, unmatched_usage, tasks)
        
        self.print_report(correlated, unmatched_tasks, unmatched_usage, stats, correlations, daily_stats)
    
    def close(self):
        self.chats_conn.close()
        self.usage_conn.close()


def main():
    parser = argparse.ArgumentParser(description='Correlate chat messages with usage API requests')
    add_db_file_argument(parser, "(chats database)")
    parser.add_argument('--usage-db-file', default=None, help='Path/pattern to usage database file(s) (default: uses --db-file if not specified)')
    
    args = parser.parse_args()
    
    chats_db = find_db_file(args.db_file)
    
    usage_db = args.usage_db_file
    if usage_db is None:
        usage_db = chats_db
        print(f"Using chats database for usage: {usage_db}")
    
    correlator = ChatUsageCorrelator(chats_db, usage_db)
    try:
        correlator.run()
    finally:
        correlator.close()


if __name__ == '__main__':
    main()

