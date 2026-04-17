"""Tests for the search API endpoints."""

import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import search as search_routes
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-search"


def _make_app(repo, embedder) -> FastAPI:
    search_routes.init(repo, embedder)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(search_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_search():
    orig_repo = search_routes._repo
    orig_emb = search_routes._embedder
    orig_last_reindex = search_routes._last_reindex
    yield
    search_routes._repo = orig_repo
    search_routes._embedder = orig_emb
    search_routes._last_reindex = orig_last_reindex


@pytest.fixture
async def client(db: Database):
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    repo = MeetingRepository(db)
    mock_embedder = MagicMock()
    mock_embedder.search.return_value = []
    mock_embedder.embed.return_value = [[0.1, 0.2, 0.3]]
    mock_embedder.embed_single.return_value = [0.1, 0.2, 0.3]
    app = _make_app(repo, mock_embedder)
    with TestClient(app) as c:
        yield c, repo, mock_embedder
    auth_mod._auth_token = original


@pytest.mark.asyncio
async def test_search_empty_query(client):
    """POST /api/search with empty query is rejected by validation."""
    c, _repo, _emb = client
    resp = c.post("/api/search", json={"query": ""}, headers=_auth_headers())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_whitespace_query(client):
    """POST /api/search with whitespace-only query returns empty results."""
    c, _repo, _emb = client
    resp = c.post("/api/search", json={"query": "   "}, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []


def test_search_no_embedder():
    """When embedder is None, returns 503."""
    search_routes._repo = MagicMock()
    search_routes._embedder = None
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = FastAPI()
        auth_deps = [Depends(verify_token)]
        app.include_router(search_routes.router, dependencies=auth_deps)
        with TestClient(app) as c:
            resp = c.post("/api/search", json={"query": "test"}, headers=_auth_headers())
            assert resp.status_code == 503
    finally:
        auth_mod._auth_token = original


@pytest.mark.asyncio
async def test_search_no_results(client):
    """Search with no embeddings in DB returns empty results."""
    c, _repo, _emb = client
    resp = c.post("/api/search", json={"query": "roadmap"}, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["query"] == "roadmap"


@pytest.mark.asyncio
async def test_search_returns_ranked_results(client):
    """Search returns correctly ranked results with metadata."""
    c, repo, mock_embedder = client

    # Create a meeting and store embeddings.
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Sprint Planning", status="complete")

    emb_records = [
        {
            "segment_index": 0,
            "embedding": [0.1, 0.2, 0.3],
            "text": "Hello everyone.",
            "speaker": "Me",
            "start_time": 0.0,
        },
        {
            "segment_index": 1,
            "embedding": [0.4, 0.5, 0.6],
            "text": "Let's discuss the roadmap.",
            "speaker": "Remote",
            "start_time": 5.0,
        },
    ]
    await repo.store_embeddings(mid, emb_records)

    # Get actual embedding IDs from DB to set up mock return.
    all_embs = await repo.get_all_embeddings()
    assert len(all_embs) == 2

    # Mock embedder.search to return the second embedding as the top result.
    mock_embedder.search.return_value = [(all_embs[1]["id"], 0.95)]

    resp = c.post(
        "/api/search",
        json={"query": "roadmap", "limit": 5},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["meeting_id"] == mid
    assert result["segment_index"] == 1
    assert result["text"] == "Let's discuss the roadmap."
    assert result["speaker"] == "Remote"
    assert result["start_time"] == 5.0
    assert result["score"] == 0.95
    assert result["meeting_title"] == "Sprint Planning"
    assert data["query"] == "roadmap"


@pytest.mark.asyncio
async def test_reindex_indexes_meetings(client):
    """Reindex endpoint indexes meetings with transcript_json."""
    c, repo, mock_embedder = client

    transcript_data = {
        "segments": [
            {"start": 0, "end": 5, "text": "Hello everyone.", "speaker": "Me"},
            {"start": 5, "end": 10, "text": "Let's discuss.", "speaker": "Remote"},
        ],
        "language": "en",
        "language_probability": 0.98,
        "duration_seconds": 10.0,
    }

    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(
        mid,
        transcript_json=json.dumps(transcript_data),
        status="complete",
    )

    # Also create a meeting without transcript (should be skipped).
    mid2 = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid2, status="complete")

    mock_embedder.embed.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    resp = c.post("/api/search/reindex", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["meetings_indexed"] == 1
    assert data["segments_indexed"] == 2

    # Verify embeddings were stored.
    all_embs = await repo.get_all_embeddings()
    assert len(all_embs) == 2
    assert all_embs[0]["meeting_id"] == mid
    assert all_embs[1]["meeting_id"] == mid


def test_reindex_no_embedder():
    """Returns 503 when embedder not available."""
    search_routes._repo = MagicMock()
    search_routes._embedder = None
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = FastAPI()
        auth_deps = [Depends(verify_token)]
        app.include_router(search_routes.router, dependencies=auth_deps)
        with TestClient(app) as c:
            resp = c.post("/api/search/reindex", headers=_auth_headers())
            assert resp.status_code == 503
    finally:
        auth_mod._auth_token = original
