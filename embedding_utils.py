#!/usr/bin/env python3
import base64
import gzip
import numpy as np


def compress_embedding(embedding: np.ndarray) -> str:
    """Compress embedding array to base64-encoded gzip string.
    
    Args:
        embedding: NumPy array of embedding values
    
    Returns:
        Base64-encoded gzip-compressed string
    """
    embedding_bytes = embedding.tobytes()
    compressed = gzip.compress(embedding_bytes)
    encoded = base64.b64encode(compressed).decode('ascii')
    return encoded


def decompress_embedding(data: str) -> np.ndarray:
    """Decompress base64-encoded gzip string to embedding array.
    
    Args:
        data: Base64-encoded gzip-compressed string
    
    Returns:
        NumPy array of embedding values
    """
    compressed = base64.b64decode(data.encode('ascii'))
    decompressed = gzip.decompress(compressed)
    return np.frombuffer(decompressed, dtype=np.float32)


def cosine_similarity(emb1: np.ndarray, emb2: np.ndarray) -> float:
    """Calculate cosine similarity between two embedding vectors.
    
    Args:
        emb1: First embedding vector
        emb2: Second embedding vector
    
    Returns:
        Cosine similarity value between -1 and 1
    """
    dot_product = np.dot(emb1, emb2)
    norm1 = np.linalg.norm(emb1)
    norm2 = np.linalg.norm(emb2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot_product / (norm1 * norm2)


def cosine_distance(emb1: np.ndarray, emb2: np.ndarray) -> float:
    """Calculate cosine distance (1 - cosine_similarity).
    
    Args:
        emb1: First embedding vector
        emb2: Second embedding vector
    
    Returns:
        Cosine distance value between 0 and 2
    """
    return 1.0 - cosine_similarity(emb1, emb2)

