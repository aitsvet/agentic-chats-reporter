#!/usr/bin/env python3
import sqlite3
import numpy as np
import sys
import argparse


def decompress_embedding(data: str) -> np.ndarray:
    import base64
    import gzip
    compressed = base64.b64decode(data.encode('ascii'))
    decompressed = gzip.decompress(compressed)
    return np.frombuffer(decompressed, dtype=np.float32)


def cosine_similarity(emb1: np.ndarray, emb2: np.ndarray) -> float:
    dot_product = np.dot(emb1, emb2)
    norm1 = np.linalg.norm(emb1)
    norm2 = np.linalg.norm(emb2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot_product / (norm1 * norm2)


def show_similarity_matrix(db_path: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    tasks = cursor.execute("""
        SELECT se.user_msg_id, se.embedding_data,
               m.message_datetime,
               (SELECT content_text FROM content WHERE message_id = se.user_msg_id) as user_content
        FROM task_embeddings se
        JOIN messages m ON se.user_msg_id = m.id
        ORDER BY m.message_datetime, m.start_line
    """).fetchall()
    
    if not tasks:
        print("No embeddings found in database")
        return
    
    embeddings = []
    labels = []
    
    for user_msg_id, embedding_data, msg_dt, user_content in tasks:
        embedding = decompress_embedding(embedding_data)
        embeddings.append(embedding)
        
        preview = user_content[:60].replace('\n', ' ') if user_content else ''
        label = f"{user_msg_id}\n{preview}..."
        labels.append(label)
    
    n = len(embeddings)
    matrix = np.zeros((n, n))
    
    print(f"\nCosine Similarity Matrix ({n}x{n})\n")
    print("=" * 100)
    
    for i in range(n):
        for j in range(n):
            similarity = cosine_similarity(embeddings[i], embeddings[j])
            matrix[i][j] = similarity
    
    print(f"\n{'Seq ID':<8}", end="")
    for i in range(n):
        print(f"{i+1:>8}", end="")
    print()
    print("-" * (8 + 8 * n))
    
    for i in range(n):
        print(f"{tasks[i][0]:<8}", end="")
        for j in range(n):
            print(f"{matrix[i][j]:>8.3f}", end="")
        print()
    
    print("\n" + "=" * 100)
    print("\nPairwise Similarities:\n")
    
    for i in range(n):
        for j in range(i + 1, n):
            sim = matrix[i][j]
            print(f"Seq {tasks[i][0]} <-> Seq {tasks[j][0]}: {sim:.4f}")
    
    print("\n" + "=" * 100)
    
    user_msg_ids = [task[0] for task in tasks]
    placeholders = ','.join(['?'] * len(user_msg_ids))
    cursor.execute(f"""
        SELECT id, message_datetime,
               (SELECT content_text FROM content WHERE message_id = id) as user_content
        FROM messages
        WHERE id IN ({placeholders})
        ORDER BY message_datetime, start_line
    """, user_msg_ids)
    
    print("\nTask Details:\n")
    details = cursor.fetchall()
    for user_msg_id, msg_dt, user_content in details:
        preview = user_content[:100].replace('\n', ' ') if user_content else ''
        print(f"Seq {user_msg_id} ({msg_dt}): {preview}...")
    
    conn.close()


def main():
    parser = argparse.ArgumentParser(description='Show cosine similarity matrix for embeddings')
    parser.add_argument('--db-file', default='EXAMPLE.db', help='Path to database file')
    
    args = parser.parse_args()
    show_similarity_matrix(args.db_file)


if __name__ == '__main__':
    main()

