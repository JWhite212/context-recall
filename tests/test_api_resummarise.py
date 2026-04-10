"""Tests for src/api/routes/resummarise.py — re-summarisation endpoint."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import resummarise as resummarise_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.summariser import MeetingSummary
from src.transcriber import Transcript

TEST_TOKEN = "test-token-for-resummarise-tests"


def _make_app(repo: MeetingRepository) -> FastAPI:
    resummarise_routes.init(repo)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(resummarise_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_repo():
    original = resummarise_routes._repo
    yield
    resummarise_routes._repo = original


@pytest.fixture
async def client(db: Database):
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    repo = MeetingRepository(db)
    app = _make_app(repo)
    with TestClient(app) as c:
        yield c, repo
    auth_mod._auth_token = original


@pytest.mark.asyncio
async def test_resummarise_missing_transcript(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, status="complete")
    # No transcript_json set.
    resp = c.post(f"/api/meetings/{mid}/resummarise", headers=_auth_headers())
    assert resp.status_code == 400
    assert "No transcript" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_resummarise_meeting_not_found(client):
    c, _ = client
    resp = c.post("/api/meetings/nonexistent/resummarise", headers=_auth_headers())
    assert resp.status_code == 404


def test_resummarise_repo_not_available():
    """If init() was never called, repo is None -> 503."""
    resummarise_routes._repo = None
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = FastAPI()
        auth_deps = [Depends(verify_token)]
        app.include_router(resummarise_routes.router, dependencies=auth_deps)
        with TestClient(app) as c:
            resp = c.post("/api/meetings/any-id/resummarise", headers=_auth_headers())
            assert resp.status_code == 503
    finally:
        auth_mod._auth_token = original


@pytest.mark.asyncio
async def test_resummarise_success(client):
    c, repo = client

    transcript_data = {
        "segments": [
            {"start": 0, "end": 5, "text": "Hello everyone."},
            {"start": 5, "end": 10, "text": "Let's discuss the roadmap."},
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
        duration_seconds=10.0,
    )

    mock_summary = MeetingSummary(
        raw_markdown="# Re-summarised\n\nNew summary.",
        title="Re-summarised Meeting",
        tags=["updated", "roadmap"],
    )

    with patch("src.api.routes.resummarise._load_summarisation_config"):
        with patch("src.api.routes.resummarise.Summariser") as mock_summariser_cls:
            mock_instance = MagicMock()
            mock_instance.summarise.return_value = mock_summary
            mock_summariser_cls.return_value = mock_instance

            resp = c.post(f"/api/meetings/{mid}/resummarise", headers=_auth_headers())
            assert resp.status_code == 200
            data = resp.json()
            assert data["meeting_id"] == mid
            assert data["title"] == "Re-summarised Meeting"
            assert data["tags"] == ["updated", "roadmap"]

    # Verify the meeting was actually updated in the DB.
    meeting = await repo.get_meeting(mid)
    assert meeting.title == "Re-summarised Meeting"
    assert meeting.tags == ["updated", "roadmap"]


def test_reconstruct_transcript():
    """Test _reconstruct_transcript directly."""
    transcript_json = json.dumps({
        "segments": [
            {"start": 0, "end": 5, "text": "Hello.", "speaker": "Me"},
            {"start": 5, "end": 10, "text": "Hi there.", "speaker": "Remote"},
        ],
        "language": "en",
        "language_probability": 0.95,
        "duration_seconds": 10.0,
    })

    transcript = resummarise_routes._reconstruct_transcript(transcript_json, duration=10.0)
    assert isinstance(transcript, Transcript)
    assert len(transcript.segments) == 2
    assert transcript.segments[0].text == "Hello."
    assert transcript.segments[0].speaker == "Me"
    assert transcript.segments[1].text == "Hi there."
    assert transcript.language == "en"
    assert transcript.duration_seconds == 10.0
