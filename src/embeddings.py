"""
Embedding infrastructure for semantic search over meeting transcripts.

Uses sentence-transformers to convert text segments into dense vectors,
then supports cosine-similarity search across the embedding space.
The model loads lazily on first use (~80MB download for all-MiniLM-L6-v2).
"""

from __future__ import annotations

import threading

import numpy as np


def is_embeddings_available() -> bool:
    """Check if sentence-transformers is installed."""
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


class Embedder:
    """Embeds text into vectors using sentence-transformers for semantic search."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model = None  # Lazy-loaded
        self._lock = threading.Lock()

    def _load_model(self) -> None:
        """Lazy-load the sentence-transformers model (~80MB download on first use)."""
        # Guard import so the module loads even without sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers is required for semantic search. "
                "Install it with: pip install sentence-transformers"
            ) from None
        self._model = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into vectors. Returns list of float lists."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()

    def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        return self.embed([text])[0]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        a_arr = np.array(a)
        b_arr = np.array(b)
        dot = np.dot(a_arr, b_arr)
        norm = float(np.linalg.norm(a_arr) * np.linalg.norm(b_arr))
        if norm < 1e-10:
            return 0.0
        return float(dot / norm)

    def search(
        self,
        query: str,
        embeddings: list[tuple[int, list[float]]],  # (id, vector) pairs
        limit: int = 10,
    ) -> list[tuple[int, float]]:
        """
        Search for the most similar embeddings to the query.

        Returns list of (id, similarity_score) pairs, sorted by score descending.
        """
        query_vec = self.embed_single(query)
        scores = []
        for emb_id, vec in embeddings:
            score = self.cosine_similarity(query_vec, vec)
            scores.append((emb_id, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:limit]
