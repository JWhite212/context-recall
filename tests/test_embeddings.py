"""Tests for the embedding infrastructure (semantic search)."""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.embeddings import Embedder, is_embeddings_available


def test_is_embeddings_available():
    """is_embeddings_available returns a bool regardless of install state."""
    result = is_embeddings_available()
    assert isinstance(result, bool)


def test_cosine_similarity_identical():
    """Identical vectors should have cosine similarity ~1.0."""
    vec = [1.0, 2.0, 3.0]
    assert Embedder.cosine_similarity(vec, vec) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    """Orthogonal vectors should have cosine similarity ~0.0."""
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert Embedder.cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_opposite():
    """Opposite vectors should have cosine similarity ~-1.0."""
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]
    assert Embedder.cosine_similarity(a, b) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector():
    """Zero vector should return 0.0 (avoid division by zero)."""
    a = [1.0, 2.0, 3.0]
    b = [0.0, 0.0, 0.0]
    assert Embedder.cosine_similarity(a, b) == 0.0


def test_embed_calls_model():
    """embed() should call model.encode() with the given texts."""
    mock_model = MagicMock()
    mock_model.encode.return_value = np.array([[0.1, 0.2], [0.3, 0.4]])

    embedder = Embedder()
    embedder._model = mock_model

    result = embedder.embed(["hello", "world"])

    mock_model.encode.assert_called_once_with(["hello", "world"], show_progress_bar=False)
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_single():
    """embed_single() should return a single vector, not a list of vectors."""
    mock_model = MagicMock()
    mock_model.encode.return_value = np.array([[0.5, 0.6, 0.7]])

    embedder = Embedder()
    embedder._model = mock_model

    result = embedder.embed_single("test text")

    assert result == [0.5, 0.6, 0.7]


def test_lazy_loading():
    """Creating an Embedder should NOT load the model; calling embed() should."""
    embedder = Embedder()
    assert embedder._model is None

    # Inject a mock to prove _load_model would set it
    mock_model = MagicMock()
    mock_model.encode.return_value = np.array([[0.1, 0.2]])

    with patch.object(embedder, "_load_model") as mock_load:
        # Simulate what _load_model does
        def side_effect():
            embedder._model = mock_model

        mock_load.side_effect = side_effect
        embedder.embed(["test"])

    mock_load.assert_called_once()
    assert embedder._model is mock_model


@pytest.mark.asyncio
async def test_bruteforce_cosine_fallback_runs_when_vec_unavailable(repo, monkeypatch):
    """search_embeddings must transparently use the brute-force cosine path
    when sqlite-vec is not loaded (`_vec_available = False`).

    This guards regressions where the vec0 query was the only path and
    silently returned nothing on systems without the sqlite-vec extension.
    """
    import time as _time

    import src.db.database as db_mod
    import src.db.repository as repo_mod

    # Force the fallback regardless of whether vec0 actually loaded.
    monkeypatch.setattr(db_mod, "_vec_available", False)

    mid = await repo.create_meeting(started_at=_time.time())
    embeddings = [
        {
            "segment_index": 0,
            "embedding": [1.0, 0.0, 0.0],
            "text": "first segment",
            "speaker": "Me",
            "start_time": 0.0,
        },
        {
            "segment_index": 1,
            "embedding": [0.0, 1.0, 0.0],
            "text": "second segment",
            "speaker": "Remote",
            "start_time": 5.0,
        },
    ]
    await repo.store_embeddings(mid, embeddings)

    # Spy on the brute-force method to prove it was the one invoked.
    called = {"n": 0}
    original_bf = repo_mod.MeetingRepository._search_embeddings_bruteforce

    async def spy_bf(self, *args, **kwargs):
        called["n"] += 1
        return await original_bf(self, *args, **kwargs)

    monkeypatch.setattr(repo_mod.MeetingRepository, "_search_embeddings_bruteforce", spy_bf)

    # Query closest to the first vector — it should rank first.
    results = await repo.search_embeddings([1.0, 0.0, 0.0], limit=2)
    assert called["n"] == 1, "Brute-force path must run when _vec_available is False"
    assert len(results) == 2
    assert results[0]["text"] == "first segment"
    assert results[0]["distance"] < results[1]["distance"]
