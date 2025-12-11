#!/usr/bin/env python3
import argparse
import sys
from typing import List, Dict, Optional, Tuple
import numpy as np
from embed_tasks import TaskEmbedder
from dotenv import load_dotenv
import os
from llm_utils import tokens_to_chars, create_openai_client, load_api_config, DEFAULT_SUMMARY_PARAMS, get_llm_params, get_llm_context_limit_and_max_tokens
from embedding_utils import decompress_embedding, cosine_distance
from db_utils import find_db_file, add_db_file_argument


class TaskClusterer:
    def __init__(self, chats_db: str, emb_url: str, emb_model: str, llm_url: str, llm_model: str, emb_api_key: str = None, llm_api_key: str = None):
        self.embedder = TaskEmbedder(chats_db, emb_url, emb_model, emb_api_key)
        self.chats_conn = self.embedder.chats_conn
        self.chats_cursor = self.embedder.chats_cursor
        self.llm_client = create_openai_client(llm_url, llm_api_key, 'LLM_API_KEY')
        self.llm_model = llm_model
        summary_params = get_llm_params('SUMMARY_PARAMS', DEFAULT_SUMMARY_PARAMS)
        context_limit, _ = get_llm_context_limit_and_max_tokens(self.llm_client, self.llm_model, summary_params)
        self.llm_context_size = int(context_limit * 0.8)
        self.prompt_overhead = self._calculate_prompt_overhead()
        
        percentile = os.getenv('CLUSTER_THRESHOLD')
        self.percentile = float(percentile) if percentile else 0.85
        
        min_size_ratio = os.getenv('CLUSTER_MIN_GROUP_SIZE_RATIO')
        self.min_size_ratio = float(min_size_ratio) if min_size_ratio else None
    
    def _calculate_prompt_overhead(self) -> int:
        from generate_group_summaries import SUMMARY_SYSTEM_PROMPT
        summary_prompt_template = os.getenv('SUMMARY_SYSTEM_PROMPT', SUMMARY_SYSTEM_PROMPT)
        prompt_extra = os.getenv('PROMPT_EXTRA', '').strip()
        formatted_prompt = summary_prompt_template.format(
            USER_SUMMARY_MAX_TOKENS=int(os.getenv('USER_SUMMARY_MAX_TOKENS', '480')),
            AGENT_SUMMARY_MAX_TOKENS=int(os.getenv('AGENT_SUMMARY_MAX_TOKENS', '320')),
            PROMPT_EXTRA=prompt_extra
        )
        return len(formatted_prompt) + 1000
    
    def load_embeddings_and_lengths(self) -> Tuple[Dict[int, np.ndarray], Dict[int, int], List[int], List[Dict]]:
        """Load embeddings, lengths, and task data ordered by timestamp."""
        tasks_data = self.chats_cursor.execute("""
            SELECT se.user_msg_id, se.embedding_data, se.formatted_length,
                   m.message_datetime, m.start_line
            FROM task_embeddings se
            JOIN messages m ON se.user_msg_id = m.id
            ORDER BY m.message_datetime, m.start_line
        """).fetchall()
        
        if not tasks_data:
            return {}, {}, [], []
        
        embeddings_map = {}
        lengths_map = {}
        ordered_user_msg_ids = []
        tasks = []
        
        for user_msg_id, embedding_data, formatted_length, msg_datetime, start_line in tasks_data:
            embedding = decompress_embedding(embedding_data)
            embeddings_map[user_msg_id] = embedding
            lengths_map[user_msg_id] = formatted_length or 0
            ordered_user_msg_ids.append(user_msg_id)
            tasks.append({
                'user_msg_id': user_msg_id,
                'formatted_length': formatted_length or 0,
                'message_datetime': msg_datetime,
                'start_line': start_line
            })
        
        return embeddings_map, lengths_map, ordered_user_msg_ids, tasks
    
    def calculate_consecutive_distances(self, embeddings_map: Dict[int, np.ndarray], task_ids: List[int]) -> List[float]:
        """Calculate distances between consecutive tasks only (O(n))."""
        distances = []
        
        for i in range(len(task_ids) - 1):
            task_id_1 = task_ids[i]
            task_id_2 = task_ids[i + 1]
            
            emb1 = embeddings_map[task_id_1]
            emb2 = embeddings_map[task_id_2]
            
            dist = cosine_distance(emb1, emb2)
            distances.append(dist)
        
        return distances
    
    def select_threshold(self, distances: List[float]) -> float:
        """Select threshold using specified percentile of consecutive distances."""
        sorted_distances = sorted(distances)
        percentile_idx = int(len(sorted_distances) * self.percentile)
        threshold = sorted_distances[percentile_idx]
        return threshold
    
    def sequential_cluster(self, tasks: List[Dict], embeddings_map: Dict[int, np.ndarray], 
                          threshold: float, max_size: int, min_size: Optional[int] = None) -> List[List[Dict]]:
        """Sequential clustering: merge consecutive tasks if similar and fits."""
        groups = []
        current_group = []
        current_size = 0
        
        for task in tasks:
            task_id = task['user_msg_id']
            task_size = task['formatted_length']
            
            can_merge = False
            
            if not current_group:
                can_merge = True
            else:
                last_task = current_group[-1]
                last_task_id = last_task['user_msg_id']
                
                distance = cosine_distance(
                    embeddings_map[task_id],
                    embeddings_map[last_task_id]
                )
                
                if distance <= threshold:
                    if (current_size + task_size) <= max_size:
                        can_merge = True
                    elif min_size and current_size < min_size:
                        can_merge = True
            
            if can_merge:
                current_group.append(task)
                current_size += task_size
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [task]
                current_size = task_size
        
        if current_group:
            groups.append(current_group)
        
        return groups
    
    def cluster_tasks(self) -> Dict[int, List[int]]:
        """Main clustering function using sequential clustering."""
        print("Loading embeddings and task lengths...")
        sys.stdout.flush()
        embeddings_map, lengths_map, all_task_ids, tasks = self.load_embeddings_and_lengths()
        
        if not all_task_ids:
            print("No tasks found with embeddings")
            return {}
        
        total_tasks = len(all_task_ids)
        
        if total_tasks == 1:
            print(f"Loaded {total_tasks} task")
            print("Only one task - creating single group")
            return {0: [all_task_ids[0]]}
        
        context_size_chars = tokens_to_chars(self.llm_context_size)
        max_cluster_size_chars = context_size_chars - self.prompt_overhead
        min_cluster_size_chars = int(max_cluster_size_chars * self.min_size_ratio) if self.min_size_ratio else None
        
        print(f"Loaded {total_tasks} tasks")
        print(f"LLM context size limit: {self.llm_context_size:,} tokens ({context_size_chars:,} characters)")
        print(f"Max cluster size: {max_cluster_size_chars:,} chars (context: {context_size_chars:,} - prompt overhead: {self.prompt_overhead:,})")
        if min_cluster_size_chars:
            print(f"Min cluster size: {min_cluster_size_chars:,} chars ({self.min_size_ratio*100:.0f}% of max)")
        print()
        sys.stdout.flush()
        
        print("Calculating consecutive distances...")
        sys.stdout.flush()
        distances = self.calculate_consecutive_distances(embeddings_map, all_task_ids)
        print(f"  Analyzed {len(distances)} consecutive pairs")
        sys.stdout.flush()
        
        print("\nSelecting threshold...")
        sys.stdout.flush()
        threshold = self.select_threshold(distances)
        
        print(f"  Percentile: {self.percentile:.2f} (CLUSTER_THRESHOLD)")
        print(f"  Selected threshold: {threshold:.4f}")
        
        distances_array = np.array(distances)
        below_threshold = np.sum(distances_array <= threshold)
        above_threshold = len(distances) - below_threshold
        
        print(f"  Consecutive pairs below threshold: {below_threshold} ({100*below_threshold/len(distances):.1f}%) - will merge into groups")
        print(f"  Consecutive pairs above threshold: {above_threshold} ({100*above_threshold/len(distances):.1f}%) - will start new groups")
        print(f"  Note: Groups continue merging consecutive tasks until threshold exceeded or size limit reached")
        print(f"  Distance range: {np.min(distances_array):.4f} to {np.max(distances_array):.4f}")
        print(f"  Distance median: {np.median(distances_array):.4f}")
        print()
        sys.stdout.flush()
        
        print("Clustering tasks sequentially...")
        sys.stdout.flush()
        groups_list = self.sequential_cluster(tasks, embeddings_map, threshold, max_cluster_size_chars, min_cluster_size_chars)
        
        final_groups = {}
        for group_id, group_tasks in enumerate(groups_list):
            final_groups[group_id] = [t['user_msg_id'] for t in group_tasks]
        
        print(f"Clustering complete: {len(final_groups)} groups")
        sys.stdout.flush()
        
        return final_groups
    
    def has_clustering_results(self) -> bool:
        """Check if clustering results already exist in database."""
        count = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM task_groups WHERE threshold = -1.0
        """).fetchone()[0]
        return count > 0
    
    def validate_clustering_results(self) -> Tuple[bool, Dict]:
        """Validate that existing clustering results are still valid."""
        groups_tasks = set(self.chats_cursor.execute("""
            SELECT DISTINCT user_msg_id FROM task_groups WHERE threshold = -1.0
        """).fetchall())
        groups_tasks = {row[0] for row in groups_tasks}
        
        embeddings_tasks = set(self.chats_cursor.execute("""
            SELECT user_msg_id FROM task_embeddings
        """).fetchall())
        embeddings_tasks = {row[0] for row in embeddings_tasks}
        
        missing_tasks = groups_tasks - embeddings_tasks
        new_tasks = embeddings_tasks - groups_tasks
        
        all_tasks_exist = len(missing_tasks) == 0
        no_new_tasks = len(new_tasks) == 0
        
        return (all_tasks_exist and no_new_tasks, {
            'all_tasks_exist': all_tasks_exist,
            'no_new_tasks': no_new_tasks,
            'new_tasks_count': len(new_tasks),
            'missing_tasks_count': len(missing_tasks),
            'groups_task_count': len(groups_tasks),
            'embeddings_task_count': len(embeddings_tasks)
        })
    
    def store_groups(self, groups: Dict[int, List[int]]):
        """Store groups in database with a special threshold value."""
        threshold = -1.0
        
        self.chats_cursor.execute("""
            DELETE FROM task_groups WHERE threshold = ?
        """, (threshold,))
        
        for group_id, user_msg_ids in groups.items():
            for user_msg_id in user_msg_ids:
                self.chats_cursor.execute("""
                    INSERT INTO task_groups (threshold, group_id, user_msg_id)
                    VALUES (?, ?, ?)
                """, (threshold, group_id, user_msg_id))
        
        self.chats_conn.commit()
    
    def calculate_group_stats(self, groups: Dict[int, List[int]]) -> Dict:
        """Calculate statistics for groups."""
        group_summary_lengths = []
        group_sizes = []
        
        for group_id, user_msg_ids in groups.items():
            group_total_length = self.chats_cursor.execute("""
                SELECT COALESCE(SUM(formatted_length), 0)
                FROM task_embeddings
                WHERE user_msg_id IN ({})
            """.format(','.join('?' * len(user_msg_ids))), user_msg_ids).fetchone()[0]
            
            if group_total_length > 0:
                group_summary_lengths.append(group_total_length)
            group_sizes.append(len(user_msg_ids))
        
        if group_summary_lengths:
            min_length = min(group_summary_lengths)
            avg_length = sum(group_summary_lengths) / len(group_summary_lengths)
            max_length = max(group_summary_lengths)
        else:
            min_length = avg_length = max_length = 0
        
        return {
            'min_length': min_length,
            'avg_length': avg_length,
            'max_length': max_length,
            'min_tasks': min(group_sizes) if group_sizes else 0,
            'avg_tasks': sum(group_sizes) / len(group_sizes) if group_sizes else 0,
            'max_tasks': max(group_sizes) if group_sizes else 0
        }
    
    def generate_report(self, groups: Dict[int, List[int]]):
        """Generate markdown report."""
        all_tasks = self.chats_cursor.execute("""
            SELECT COUNT(*), MIN(message_count), AVG(message_count), MAX(message_count)
            FROM task_embeddings
        """).fetchone()
        
        total_tasks, min_msg, avg_msg, max_msg = all_tasks
        total_tasks = total_tasks or 0
        min_msg = int(min_msg) if min_msg else 0
        avg_msg = float(avg_msg) if avg_msg else 0.0
        max_msg = int(max_msg) if max_msg else 0
        
        stats = self.calculate_group_stats(groups)
        
        print("\n## Task Clustering Report\n")
        context_size_chars = tokens_to_chars(self.llm_context_size)
        effective_limit = context_size_chars - self.prompt_overhead
        print(f"**Clustering Method:** Sequential (consecutive tasks only)")
        print(f"**Total Tasks:** {total_tasks}")
        print(f"**Message Count - Min:** {min_msg}, **Avg:** {avg_msg:.1f}, **Max:** {max_msg}\n")
        print(f"**LLM Context Size Limit:** {self.llm_context_size:,} tokens ({context_size_chars:,} characters)")
        print(f"**Effective Cluster Size Limit:** {effective_limit:,} characters (after {self.prompt_overhead:,} chars prompt overhead)\n")
        
        print("### Clustering Statistics\n")
        print("| Metric | Value |")
        print("|--------|-------|")
        print(f"| Total Groups | {len(groups)} |")
        print(f"| Min Tasks per Group | {stats['min_tasks']} |")
        print(f"| Avg Tasks per Group | {stats['avg_tasks']:.1f} |")
        print(f"| Max Tasks per Group | {stats['max_tasks']} |")
        print(f"| Min Group Size (chars) | {stats['min_length']:,} |")
        print(f"| Avg Group Size (chars) | {stats['avg_length']:,.0f} |")
        print(f"| Max Group Size (chars) | {stats['max_length']:,} |")
        if stats['min_length'] > 0:
            size_ratio = stats['max_length'] / stats['min_length']
            print(f"| Size Ratio (max/min) | {size_ratio:.1f}x |")
        print()
    
    def cleanup_orphaned_groups(self):
        """Remove task_groups entries that reference non-existent messages."""
        orphaned_count = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM task_groups tg
            LEFT JOIN messages m ON tg.user_msg_id = m.id
            WHERE m.id IS NULL
        """).fetchone()[0]
        
        if orphaned_count > 0:
            orphaned_groups = self.chats_cursor.execute("""
                SELECT COUNT(DISTINCT group_id) FROM task_groups tg
                LEFT JOIN messages m ON tg.user_msg_id = m.id
                WHERE m.id IS NULL
            """).fetchone()[0]
            print(f"Found {orphaned_count} orphaned task_groups entries in {orphaned_groups} groups")
            print(f"  These likely occurred because messages were deleted during re-parsing")
            print(f"  Cleaning up orphaned entries...")
            
            self.chats_cursor.execute("""
                DELETE FROM task_groups
                WHERE user_msg_id NOT IN (SELECT id FROM messages)
            """)
            self.chats_conn.commit()
            print(f"  Cleaned up {orphaned_count} orphaned task_groups entries")
        else:
            print("No orphaned task_groups entries found")
    
    def run(self, skip_if_exists: bool = True):
        """Run clustering."""
        self.cleanup_orphaned_groups()
        
        if skip_if_exists and self.has_clustering_results():
            is_valid, stats = self.validate_clustering_results()
            
            group_count = self.chats_cursor.execute("""
                SELECT COUNT(DISTINCT group_id) FROM task_groups WHERE threshold = -1.0
            """).fetchone()[0]
            task_count = self.chats_cursor.execute("""
                SELECT COUNT(*) FROM task_groups WHERE threshold = -1.0
            """).fetchone()[0]
            
            if is_valid:
                print(f"Skipping clustering: {group_count} groups with {task_count} tasks already exist and are valid")
                print(f"  All {stats['groups_task_count']} tasks in groups exist, no new tasks found")
                return
            else:
                print(f"Existing clustering results are invalid:")
                if stats['missing_tasks_count'] > 0:
                    print(f"  {stats['missing_tasks_count']} tasks in groups no longer exist (deleted)")
                if stats['new_tasks_count'] > 0:
                    print(f"  {stats['new_tasks_count']} new tasks found (not in any group)")
                print(f"  Re-clustering required...")
                print()
        
        sys.stdout.flush()
        groups = self.cluster_tasks()
        
        stats = self.calculate_group_stats(groups)
        
        context_size_chars = tokens_to_chars(self.llm_context_size)
        effective_limit = context_size_chars - self.prompt_overhead
        
        oversized_groups = []
        for group_id, user_msg_ids in groups.items():
            group_total_length = self.chats_cursor.execute("""
                SELECT COALESCE(SUM(formatted_length), 0)
                FROM task_embeddings
                WHERE user_msg_id IN ({})
            """.format(','.join('?' * len(user_msg_ids))), user_msg_ids).fetchone()[0]
            if group_total_length > effective_limit:
                oversized_groups.append((group_id, len(user_msg_ids), group_total_length))
        
        if oversized_groups:
            print(f"\nWARNING: {len(oversized_groups)} group(s) exceed the effective limit ({effective_limit:,} chars):")
            for group_id, task_count, group_size in sorted(oversized_groups, key=lambda x: x[2], reverse=True)[:5]:
                print(f"  Group {group_id}: {task_count} tasks, {group_size:,} chars (exceeds by {group_size - effective_limit:,} chars)")
            if len(oversized_groups) > 5:
                print(f"  ... and {len(oversized_groups) - 5} more oversized groups")
            print(f"  These groups may fail during summarization. Consider re-running with --force to regenerate.")
            sys.stdout.flush()
        
        print(f"\nStoring {len(groups)} final groups in database...")
        sys.stdout.flush()
        self.store_groups(groups)
        
        total_tasks = sum(len(g) for g in groups.values())
        
        embeddings_map, lengths_map, all_task_ids, tasks = self.load_embeddings_and_lengths()
        expected_count = len(all_task_ids)
        
        if total_tasks != expected_count:
            print(f"\nWARNING: Task count mismatch!")
            print(f"  Expected: {expected_count} tasks (from embeddings)")
            print(f"  Found in groups: {total_tasks} tasks")
            print(f"  Missing: {expected_count - total_tasks} tasks")
            print(f"  Coverage: {100*total_tasks/expected_count:.1f}%")
        
        print(f"\nCompleted: {len(groups)} groups, {total_tasks} tasks")
        print(f"Group sizes - Min: {stats['min_tasks']}, Avg: {stats['avg_tasks']:.1f}, Max: {stats['max_tasks']}")
        print(f"Group summary lengths - Min: {stats['min_length']:,.0f}, Avg: {stats['avg_length']:,.0f}, Max: {stats['max_length']:,.0f}")
        if stats['min_length'] > 0:
            size_ratio = stats['max_length'] / stats['min_length']
            print(f"Size ratio (max/min): {size_ratio:.1f}x")
        if oversized_groups:
            print(f"Effective limit: {effective_limit:,} chars (after {self.prompt_overhead:,} chars prompt overhead)")
        sys.stdout.flush()
        
        self.generate_report(groups)
    
    def close(self):
        self.embedder.close()


def main():
    load_dotenv()
    
    config = load_api_config(['emb_url', 'emb_model', 'llm_url', 'llm_model'])
    
    parser = argparse.ArgumentParser(description='Cluster tasks by similarity using Sequential Clustering')
    add_db_file_argument(parser)
    parser.add_argument('--force', action='store_true', help='Force re-clustering even if results exist')
    
    args = parser.parse_args()
    
    chats_db = find_db_file(args.db_file)
    
    clusterer = TaskClusterer(chats_db, config['emb_url'], config['emb_model'], config['llm_url'], config['llm_model'], config['emb_api_key'], config['llm_api_key'])
    try:
        clusterer.run(skip_if_exists=not args.force)
    finally:
        clusterer.close()


if __name__ == '__main__':
    main()
