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


def test_search_returns_sorted():
    """search() should return results sorted by similarity score descending."""
    embedder = Embedder()

    # Pre-set query embedding via mock
    mock_model = MagicMock()
    # The query will be embedded as [1, 0, 0]
    mock_model.encode.return_value = np.array([[1.0, 0.0, 0.0]])
    embedder._model = mock_model

    # Provide pre-computed embeddings with varying similarity to [1, 0, 0]
    test_embeddings = [
        (1, [0.0, 1.0, 0.0]),  # orthogonal => ~0.0
        (2, [1.0, 0.0, 0.0]),  # identical => ~1.0
        (3, [0.7, 0.7, 0.0]),  # partial => ~0.707
    ]

    results = embedder.search("query", test_embeddings, limit=3)

    # Should be sorted: id=2 (1.0), id=3 (~0.707), id=1 (0.0)
    assert len(results) == 3
    assert results[0][0] == 2
    assert results[0][1] == pytest.approx(1.0)
    assert results[1][0] == 3
    assert results[2][0] == 1
    assert results[2][1] == pytest.approx(0.0)


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
