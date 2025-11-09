#!/usr/bin/env python3
import sqlite3
import argparse
import sys
from typing import List, Dict, Optional, Tuple, Set
import numpy as np
from embed_tasks import TaskEmbedder
from openai import OpenAI
from dotenv import load_dotenv
import os
from collections import defaultdict
from llm_utils import get_model_context_size, tokens_to_chars


class TaskClusterer:
    def __init__(self, chats_db: str, emb_url: str, emb_model: str, llm_url: str, llm_model: str, emb_api_key: str = None, llm_api_key: str = None, sequence_weight: float = None):
        self.embedder = TaskEmbedder(chats_db, emb_url, emb_model, emb_api_key)
        self.chats_conn = self.embedder.chats_conn
        self.chats_cursor = self.embedder.chats_cursor
        api_key = llm_api_key or os.getenv('LLM_API_KEY', 'not-needed')
        self.llm_client = OpenAI(base_url=llm_url, api_key=api_key)
        self.llm_model = llm_model
        self.llm_context_size = self._get_llm_context_size()
        self.sequence_weight = sequence_weight if sequence_weight is not None else float(os.getenv('CLUSTER_SEQUENCE_WEIGHT', '1.0'))
    
    def _get_llm_context_size(self) -> int:
        context_size = get_model_context_size(self.llm_client, self.llm_model, model_type='llm')
        return int(context_size * 0.8)
    
    def cosine_distance(self, emb1: np.ndarray, emb2: np.ndarray) -> float:
        """Calculate cosine distance (1 - cosine_similarity)."""
        return 1.0 - self.embedder.cosine_similarity(emb1, emb2)
    
    def decompress_embedding(self, data: str) -> np.ndarray:
        return self.embedder.decompress_embedding(data)
    
    def load_embeddings_and_lengths(self, user_msg_ids: Optional[List[int]] = None) -> Tuple[Dict[int, np.ndarray], Dict[int, int], List[int]]:
        """Load embeddings and formatted lengths for tasks."""
        if user_msg_ids is None:
            tasks_data = self.chats_cursor.execute("""
                SELECT se.user_msg_id, se.embedding_data, se.formatted_length
                FROM task_embeddings se
                JOIN messages m ON se.user_msg_id = m.id
                ORDER BY m.message_datetime, m.start_line
            """).fetchall()
        else:
            placeholders = ','.join('?' * len(user_msg_ids))
            tasks_data = self.chats_cursor.execute(f"""
                SELECT se.user_msg_id, se.embedding_data, se.formatted_length
                FROM task_embeddings se
                JOIN messages m ON se.user_msg_id = m.id
                WHERE se.user_msg_id IN ({placeholders})
                ORDER BY m.message_datetime, m.start_line
            """, user_msg_ids).fetchall()
        
        if not tasks_data:
            return {}, {}, []
        
        embeddings_map = {}
        lengths_map = {}
        ordered_user_msg_ids = []
        for user_msg_id, embedding_data, formatted_length in tasks_data:
            embedding = self.decompress_embedding(embedding_data)
            embeddings_map[user_msg_id] = embedding
            lengths_map[user_msg_id] = formatted_length or 0
            ordered_user_msg_ids.append(user_msg_id)
        
        return embeddings_map, lengths_map, ordered_user_msg_ids
    
    def calculate_cluster_size(self, cluster_ids: List[int], lengths_map: Dict[int, int]) -> int:
        """Calculate total character size of a cluster."""
        return sum(lengths_map.get(task_id, 0) for task_id in cluster_ids)
    
    def compute_distance_matrix(self, embeddings_map: Dict[int, np.ndarray], task_ids: List[int]) -> np.ndarray:
        """Compute full pairwise distance matrix with sequence-based penalty."""
        n = len(task_ids)
        if n < 2:
            return np.zeros((n, n))
        
        total_pairs = n * (n - 1) // 2
        print(f"  Computing pairwise distance matrix for {n} tasks ({total_pairs:,} pairs)...")
        print(f"  Sequence weight: {self.sequence_weight:.3f}")
        sys.stdout.flush()
        
        distance_matrix = np.zeros((n, n))
        last_progress = 0
        
        max_sequence_distance = n - 1
        
        for i in range(n):
            for j in range(i + 1, n):
                cosine_dist = self.cosine_distance(embeddings_map[task_ids[i]], embeddings_map[task_ids[j]])
                
                sequence_distance = abs(i - j)
                normalized_sequence_distance = sequence_distance / max_sequence_distance if max_sequence_distance > 0 else 0
                
                adjusted_dist = cosine_dist + self.sequence_weight * normalized_sequence_distance
                
                distance_matrix[i, j] = adjusted_dist
                distance_matrix[j, i] = adjusted_dist
            
            progress = int((i + 1) * 100 / n)
            if progress >= last_progress + 10:
                pairs_computed = (i + 1) * i // 2 + (i + 1)
                print(f"  Progress: {progress}% ({pairs_computed:,} pairs computed)")
                sys.stdout.flush()
                last_progress = progress
        
        print(f"  Completed distance matrix computation")
        sys.stdout.flush()
        return distance_matrix
    
    def find_optimal_threshold(self, distance_matrix: np.ndarray, task_ids: List[int], 
                               target_cluster_ratio: float = 0.85) -> float:
        """Find optimal distance threshold using k-distance graph analysis.
        
        Uses the k-distance graph method to find a natural break point in the distance
        distribution, which typically corresponds to a good clustering threshold.
        """
        n = len(task_ids)
        if n < 2:
            return 0.5
        
        print("  Analyzing distance matrix for optimal threshold using k-distance graph...")
        sys.stdout.flush()
        
        k = min(10, max(3, n // 20))
        
        k_distances = []
        for i in range(n):
            row_distances = []
            for j in range(n):
                if i != j:
                    row_distances.append(distance_matrix[i, j])
            row_distances.sort()
            if len(row_distances) >= k:
                k_distances.append(row_distances[k - 1])
            elif row_distances:
                k_distances.append(row_distances[-1])
        
        if not k_distances:
            all_distances = []
            for i in range(n):
                for j in range(i + 1, n):
                    all_distances.append(distance_matrix[i, j])
            if not all_distances:
                return 0.5
            all_distances.sort()
            percentile_idx = int(len(all_distances) * 0.2)
            threshold = all_distances[min(percentile_idx, len(all_distances) - 1)]
        else:
            k_distances.sort()
            
            percentile_idx = int(len(k_distances) * 0.2)
            threshold = k_distances[min(percentile_idx, len(k_distances) - 1)]
            
            all_distances = []
            for i in range(n):
                for j in range(i + 1, n):
                    all_distances.append(distance_matrix[i, j])
            all_distances.sort()
            
            min_threshold = all_distances[int(len(all_distances) * 0.05)]
            max_threshold = all_distances[int(len(all_distances) * 0.5)]
            
            threshold = max(min_threshold, min(threshold, max_threshold))
        
        print(f"  Optimal threshold: {threshold:.4f} (cosine distance)")
        print(f"  This corresponds to cosine similarity > {1-threshold:.4f}")
        sys.stdout.flush()
        
        return threshold
    
    def agglomerative_cluster(self, task_ids: List[int], distance_matrix: np.ndarray,
                              task_id_to_index: Dict[int, int], lengths_map: Dict[int, int],
                              distance_threshold: float, max_cluster_size: int) -> Tuple[Dict[int, List[int]], int]:
        """Agglomerative hierarchical clustering with distance threshold and size constraints.
        
        Ensures all tasks are grouped. Uses average-linkage clustering.
        Returns (final_groups, initial_cluster_count) where initial_cluster_count is the
        number of clusters before size-based splitting.
        """
        n = len(task_ids)
        if n == 0:
            return {}, 0
        if n == 1:
            return {0: task_ids}, 1
        
        print(f"  Clustering {n} tasks with distance threshold {distance_threshold:.4f}...")
        sys.stdout.flush()
        
        from sklearn.cluster import AgglomerativeClustering
        
        indices = [task_id_to_index[task_id] for task_id in task_ids]
        submatrix = distance_matrix[np.ix_(indices, indices)]
        
        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance_threshold,
            linkage='average',
            metric='precomputed'
        )
        
        labels = clustering.fit_predict(submatrix)
        
        groups = defaultdict(list)
        for i, task_id in enumerate(task_ids):
            groups[labels[i]].append(task_id)
        
        initial_cluster_count = len(groups)
        print(f"  Initial agglomerative clustering produced {initial_cluster_count} clusters")
        sys.stdout.flush()
        
        final_groups = {}
        next_group_id = 0
        
        for cluster_id, cluster_tasks in groups.items():
            cluster_size = self.calculate_cluster_size(cluster_tasks, lengths_map)
            
            if cluster_size <= max_cluster_size:
                final_groups[next_group_id] = cluster_tasks
                next_group_id += 1
            else:
                print(f"    Cluster {cluster_id} too large ({cluster_size:,} chars > {max_cluster_size:,} limit), splitting...")
                sub_groups = self.split_cluster(cluster_tasks, distance_matrix, task_id_to_index, 
                                               lengths_map, max_cluster_size)
                for sub_tasks in sub_groups:
                    final_groups[next_group_id] = sub_tasks
                    next_group_id += 1
        
        return final_groups, initial_cluster_count
    
    def split_cluster(self, cluster_tasks: List[int], distance_matrix: np.ndarray,
                     task_id_to_index: Dict[int, int], lengths_map: Dict[int, int],
                     max_cluster_size: int, depth: int = 0) -> List[List[int]]:
        """Split a cluster that exceeds size limit using size-aware splitting."""
        if len(cluster_tasks) <= 1:
            return [cluster_tasks]
        
        cluster_size = self.calculate_cluster_size(cluster_tasks, lengths_map)
        if cluster_size <= max_cluster_size:
            return [cluster_tasks]
        
        indent = "      " + "  " * depth
        print(f"{indent}Splitting cluster of {len(cluster_tasks)} tasks (size: {cluster_size:,} chars, limit: {max_cluster_size:,})...")
        sys.stdout.flush()
        
        min_cluster_size = max_cluster_size * 0.5
        estimated_clusters = max(2, int(cluster_size / min_cluster_size) + 1)
        estimated_clusters = min(estimated_clusters, len(cluster_tasks))
        
        indices = [task_id_to_index[task_id] for task_id in cluster_tasks]
        submatrix = distance_matrix[np.ix_(indices, indices)]
        
        from sklearn.cluster import KMeans
        
        best_split = None
        best_max_subsize = float('inf')
        
        for n_clusters in range(estimated_clusters, min(estimated_clusters + 3, len(cluster_tasks) + 1)):
            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = kmeans.fit_predict(submatrix)
            
            sub_groups = defaultdict(list)
            for i, task_id in enumerate(cluster_tasks):
                sub_groups[labels[i]].append(task_id)
            
            max_subsize = 0
            for sub_cluster_tasks in sub_groups.values():
                sub_size = self.calculate_cluster_size(sub_cluster_tasks, lengths_map)
                max_subsize = max(max_subsize, sub_size)
            
            if max_subsize < best_max_subsize:
                best_max_subsize = max_subsize
                best_split = dict(sub_groups)
            
            if max_subsize <= max_cluster_size:
                break
        
        if best_split is None:
            best_split = {0: cluster_tasks}
        
        result = []
        for sub_cluster_tasks in best_split.values():
            sub_size = self.calculate_cluster_size(sub_cluster_tasks, lengths_map)
            if sub_size > max_cluster_size:
                result.extend(self.split_cluster(sub_cluster_tasks, distance_matrix, 
                                               task_id_to_index, lengths_map, max_cluster_size, depth + 1))
            else:
                result.append(sub_cluster_tasks)
        
        return result
    
    def cluster_tasks(self) -> Dict[int, List[int]]:
        """Main clustering function using iterative agglomerative hierarchical clustering."""
        print(f"Loading embeddings and task lengths...")
        sys.stdout.flush()
        embeddings_map, lengths_map, all_task_ids = self.load_embeddings_and_lengths()
        
        if not all_task_ids:
            print("No tasks found with embeddings")
            return {}
        
        total_tasks = len(all_task_ids)
        context_size_chars = tokens_to_chars(self.llm_context_size)
        print(f"Loaded {total_tasks} tasks")
        print(f"LLM context size limit: {self.llm_context_size:,} tokens ({context_size_chars:,} characters)")
        print()
        sys.stdout.flush()
        
        print("Computing distance matrix...")
        sys.stdout.flush()
        distance_matrix = self.compute_distance_matrix(embeddings_map, all_task_ids)
        
        task_id_to_index = {task_id: i for i, task_id in enumerate(all_task_ids)}
        
        print("\nFinding optimal distance threshold...")
        sys.stdout.flush()
        initial_threshold = self.find_optimal_threshold(distance_matrix, all_task_ids, target_cluster_ratio=0.85)
        print(f"Initial threshold: {initial_threshold:.4f}")
        print()
        sys.stdout.flush()
        
        all_distances = []
        for i in range(len(all_task_ids)):
            for j in range(i + 1, len(all_task_ids)):
                all_distances.append(distance_matrix[i, j])
        all_distances.sort()
        
        threshold = initial_threshold
        max_iterations = 5
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            print(f"Performing agglomerative clustering (iteration {iteration})...")
            print(f"  Using threshold: {threshold:.4f}")
            sys.stdout.flush()
            
            max_cluster_size_chars = tokens_to_chars(self.llm_context_size)
            final_groups, initial_cluster_count = self.agglomerative_cluster(
                all_task_ids, distance_matrix, task_id_to_index, lengths_map,
                threshold, max_cluster_size_chars
            )
            
            if not final_groups:
                break
            
            if initial_cluster_count <= 3:
                min_threshold_idx = int(len(all_distances) * 0.05)
                current_threshold_idx = next((i for i, d in enumerate(all_distances) if d >= threshold), len(all_distances) - 1)
                
                if current_threshold_idx > min_threshold_idx:
                    new_threshold_idx = max(min_threshold_idx, int(current_threshold_idx * 0.7))
                    threshold = all_distances[new_threshold_idx]
                    print(f"  Agglomerative clustering produced only {initial_cluster_count} cluster(s), tightening threshold to {threshold:.4f}...")
                    print()
                    continue
                else:
                    print(f"  Threshold already at minimum, proceeding with splitting...")
                    break
            
            largest_cluster_size = 0
            largest_cluster_tasks = 0
            for group_tasks in final_groups.values():
                cluster_size = self.calculate_cluster_size(group_tasks, lengths_map)
                if cluster_size > largest_cluster_size:
                    largest_cluster_size = cluster_size
                    largest_cluster_tasks = len(group_tasks)
            
            total_clusters = len(final_groups)
            single_cluster_ratio = largest_cluster_tasks / total_tasks if total_tasks > 0 else 0
            
            print(f"  Result: {total_clusters} clusters (after splitting), largest has {largest_cluster_tasks} tasks ({single_cluster_ratio*100:.1f}%)")
            break
        
        if iteration >= max_iterations:
            print(f"  Reached max iterations, using current clustering result")
        
        print()
        return final_groups
    
    def has_clustering_results(self) -> bool:
        """Check if clustering results already exist in database."""
        count = self.chats_cursor.execute("""
            SELECT COUNT(*) FROM task_groups WHERE threshold = -1.0
        """).fetchone()[0]
        return count > 0
    
    def validate_clustering_results(self) -> Tuple[bool, Dict]:
        """
        Validate that existing clustering results are still valid.
        Returns (is_valid, stats_dict) where stats_dict contains:
        - all_tasks_exist: bool
        - new_tasks_count: int
        - missing_tasks_count: int
        - groups_task_count: int
        - embeddings_task_count: int
        """
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
        print(f"**Total Tasks:** {total_tasks}")
        print(f"**Message Count - Min:** {min_msg}, **Avg:** {avg_msg:.1f}, **Max:** {max_msg}\n")
        print(f"**LLM Context Size Limit:** {self.llm_context_size:,} tokens ({context_size_chars:,} characters)\n")
        
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
        
        print(f"\nStoring {len(groups)} final groups in database...")
        sys.stdout.flush()
        self.store_groups(groups)
        
        total_tasks = sum(len(g) for g in groups.values())
        
        embeddings_map, lengths_map, all_task_ids = self.load_embeddings_and_lengths()
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
        sys.stdout.flush()
        
        self.generate_report(groups)
    
    def close(self):
        self.embedder.close()


def main():
    load_dotenv()
    
    emb_url = os.getenv('EMB_URL')
    emb_model = os.getenv('EMB_MODEL')
    emb_api_key = os.getenv('EMB_API_KEY')
    llm_url = os.getenv('LLM_URL')
    llm_model = os.getenv('LLM_MODEL')
    llm_api_key = os.getenv('LLM_API_KEY')
    
    if not emb_url or not emb_model:
        raise ValueError("EMB_URL and EMB_MODEL must be set in environment or .env file")
    
    if not llm_url or not llm_model:
        raise ValueError("LLM_URL and LLM_MODEL must be set in environment or .env file")
    
    parser = argparse.ArgumentParser(description='Cluster tasks by similarity using Agglomerative Hierarchical Clustering')
    parser.add_argument('--db-file', default=None, help='Path to database file (default: searches for *.db files, uses most recent)')
    parser.add_argument('--sequence-weight', type=float, default=None, help='Weight for sequence distance penalty (default: from CLUSTER_SEQUENCE_WEIGHT env var or 1.0)')
    parser.add_argument('--force', action='store_true', help='Force re-clustering even if results exist')
    
    args = parser.parse_args()
    
    chats_db = args.db_file
    if chats_db is None:
        import glob
        db_files = glob.glob('*.db')
        if not db_files:
            raise ValueError("No database files found. Please specify --db-file or create a database with parse_chats.py")
        chats_db = max(db_files, key=os.path.getmtime)
        print(f"Using most recent database: {chats_db}")
    
    clusterer = TaskClusterer(chats_db, emb_url, emb_model, llm_url, llm_model, emb_api_key, llm_api_key, args.sequence_weight)
    try:
        clusterer.run(skip_if_exists=not args.force)
    finally:
        clusterer.close()


if __name__ == '__main__':
    main()

