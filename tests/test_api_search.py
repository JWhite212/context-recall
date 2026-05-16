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
    # Clear the rate-limit bucket so per-test caps are independent.
    search_routes._rate_buckets.clear()
    yield
    search_routes._repo = orig_repo
    search_routes._embedder = orig_emb
    search_routes._last_reindex = orig_last_reindex
    search_routes._rate_buckets.clear()


@pytest.fixture
async def client(db: Database):
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    repo = MeetingRepository(db)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [[0.1, 0.2, 0.3]]
    mock_embedder.embed_single.return_value = [0.1, 0.2, 0.3]
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

    # Search now uses repo.search_hybrid() / repo.search_embeddings() directly
    # instead of _embedder.search(). The mock embedder only needs embed_single().
    resp = c.post(
        "/api/search",
        json={"query": "roadmap", "limit": 5, "mode": "semantic"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) >= 1
    # Results should reference our meeting.
    result = data["results"][0]
    assert result["meeting_id"] == mid
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


@pytest.mark.asyncio
async def test_search_rate_limit_429(client):
    """Per-IP rate limit returns 429 once the token bucket is exhausted."""
    c, _repo, _emb = client
    # First 10 requests should pass (capacity 10), the 11th in the same
    # millisecond should be rejected.
    success = 0
    rejected = 0
    for _ in range(15):
        resp = c.post("/api/search", json={"query": "test"}, headers=_auth_headers())
        if resp.status_code == 200:
            success += 1
        elif resp.status_code == 429:
            rejected += 1
    assert success >= 1, "Expected at least one success before bucket drains"
    assert rejected >= 1, "Expected at least one 429 after exceeding capacity"


@pytest.mark.asyncio
async def test_search_batches_meeting_metadata_fetch(client, monkeypatch):
    """Search must fetch meeting metadata in a single batch, not N+1.

    Counts how many times the per-id `get_meeting` is invoked when results
    span multiple meetings — should be 0 (the batched path handles it).
    """
    c, repo, mock_embedder = client

    # Two distinct meetings with one segment each so raw_results has two
    # different meeting_ids and the N+1 path would call get_meeting twice.
    mid_a = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid_a, title="Alpha", status="complete")
    mid_b = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid_b, title="Beta", status="complete")

    await repo.store_embeddings(
        mid_a,
        [
            {
                "segment_index": 0,
                "embedding": [0.1, 0.2, 0.3],
                "text": "alpha line",
                "speaker": "Me",
                "start_time": 0.0,
            }
        ],
    )
    await repo.store_embeddings(
        mid_b,
        [
            {
                "segment_index": 0,
                "embedding": [0.4, 0.5, 0.6],
                "text": "beta line",
                "speaker": "Me",
                "start_time": 0.0,
            }
        ],
    )

    # Count direct per-id get_meeting calls.
    original_get_meeting = repo.get_meeting
    call_count = {"n": 0}

    async def counting_get_meeting(mid):
        call_count["n"] += 1
        return await original_get_meeting(mid)

    monkeypatch.setattr(repo, "get_meeting", counting_get_meeting)

    resp = c.post(
        "/api/search",
        json={"query": "line", "limit": 5, "mode": "semantic"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    # Both meetings should show with titles populated via the batch fetch.
    titles = {r["meeting_title"] for r in data["results"]}
    assert titles == {"Alpha", "Beta"}
    # No N+1: the route should not call get_meeting per result.
    assert call_count["n"] == 0, (
        f"Expected batched metadata fetch (0 get_meeting calls) but saw {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_reindex_skips_corrupt_transcript(client):
    """Corrupt transcript_json in one meeting must not abort the whole reindex."""
    c, repo, mock_embedder = client

    good_data = {
        "segments": [
            {"start": 0, "end": 5, "text": "Hello.", "speaker": "Me"},
        ],
    }
    good_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(good_id, transcript_json=json.dumps(good_data), status="complete")

    bad_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(bad_id, transcript_json="{this is not valid json", status="complete")

    mock_embedder.embed.return_value = [[0.1, 0.2, 0.3]]
    resp = c.post("/api/search/reindex", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    # The good meeting still gets indexed; the corrupt one is skipped.
    assert data["meetings_indexed"] == 1
    assert data["segments_indexed"] == 1
