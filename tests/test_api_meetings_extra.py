"""Additional tests for src/api/routes/meetings.py — supplements test_api.py."""

import time
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import meetings as meetings_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.utils.config import AppConfig, MarkdownConfig, NotionConfig

TEST_TOKEN = "test-token-for-meetings-extra"


def _make_app(repo: MeetingRepository) -> FastAPI:
    app = FastAPI()
    meetings_routes.init(repo)
    auth_deps = [Depends(verify_token)]
    app.include_router(meetings_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


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
async def test_audio_endpoint_file_not_found(client):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    # No audio_path set at all.
    resp = c.get(f"/api/meetings/{mid}/audio", headers=_auth_headers())
    assert resp.status_code == 404
    assert "Audio file not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_audio_endpoint_path_traversal_blocked(client, tmp_path):
    c, repo = client
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, audio_path="/etc/passwd")

    # Patch load_config so the audio_dir resolves to tmp_path (not /etc).
    with patch("src.api.routes.meetings.load_config") as mock_config:
        mock_config.return_value.audio.temp_audio_dir = str(tmp_path)
        resp = c.get(f"/api/meetings/{mid}/audio", headers=_auth_headers())
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_meeting_removes_audio_file(client, tmp_path):
    c, repo = client

    # Create a fake audio file.
    audio_file = tmp_path / "meeting.wav"
    audio_file.write_bytes(b"RIFF" + b"\x00" * 40)
    assert audio_file.exists()

    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, audio_path=str(audio_file))

    # Patch config so tmp_path is in the allowed audio directories.
    with patch("src.api.routes.meetings.load_config") as mock_config:
        mock_config.return_value.audio.temp_audio_dir = str(tmp_path)
        resp = c.delete(f"/api/meetings/{mid}", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not audio_file.exists()


@pytest.mark.asyncio
async def test_list_meetings_search_query(client):
    c, repo = client
    now = time.time()

    mid1 = await repo.create_meeting(started_at=now)
    await repo.update_meeting(mid1, title="Sprint Planning Alpha")

    mid2 = await repo.create_meeting(started_at=now + 1)
    await repo.update_meeting(mid2, title="Budget Review Beta")

    # Drop FTS table so search_meetings falls back to LIKE on title.
    await repo._db.conn.execute("DROP TABLE IF EXISTS meetings_fts")
    await repo._db.conn.commit()

    resp = c.get("/api/meetings?q=Sprint", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    titles = [m["title"] for m in data["meetings"]]
    assert "Sprint Planning Alpha" in titles
    assert "Budget Review Beta" not in titles


@pytest.mark.asyncio
async def test_get_meeting_tags_returns_distinct(client):
    c, repo = client
    m1 = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(m1, tags=["acme", "budget"])
    resp = c.get("/api/meetings/tags", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["acme", "budget"]


@pytest.mark.asyncio
async def test_patch_meeting_tags_persists_and_normalises(client):
    c, repo = client
    m1 = await repo.create_meeting(started_at=1000.0)
    resp = c.patch(
        f"/api/meetings/{m1}/tags",
        headers=_auth_headers(),
        json={"tags": ["  budget ", "budget", "", "planning"]},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["budget", "planning"]
    meeting = await repo.get_meeting(m1)
    assert meeting.tags == ["budget", "planning"]


@pytest.mark.asyncio
async def test_patch_meeting_tags_missing_meeting_404s(client):
    c, _ = client
    resp = c.patch("/api/meetings/nope/tags", headers=_auth_headers(), json={"tags": ["x"]})
    assert resp.status_code == 404


class _RecordingBus:
    """Tiny event-bus stub for rename tests — records emitted events."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


@pytest.fixture
async def meetings_app(db: Database, monkeypatch):
    """Unauthenticated meetings app wired with a recording event bus and
    both output writers disabled, so propagation is a no-op."""
    repo = MeetingRepository(db)
    bus = _RecordingBus()
    disabled_config = AppConfig(
        markdown=MarkdownConfig(enabled=False), notion=NotionConfig(enabled=False)
    )
    monkeypatch.setattr(meetings_routes, "load_config", lambda: disabled_config)
    app = FastAPI()
    meetings_routes.init(repo, event_bus=bus)
    app.include_router(meetings_routes.router)
    return app, repo, bus


@pytest.mark.asyncio
async def test_patch_meeting_title_sets_manual(meetings_app):
    app, repo, bus = meetings_app
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title="Auto Name", title_source="auto")

    client = TestClient(app)
    resp = client.patch(f"/api/meetings/{mid}", json={"title": "My Real Name"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "My Real Name"
    assert body["title_source"] == "manual"

    m = await repo.get_meeting(mid)
    assert m.title == "My Real Name"
    assert m.title_source == "manual"
    assert any(e["type"] == "meeting.renamed" and e["meeting_id"] == mid for e in bus.events)


@pytest.mark.asyncio
async def test_patch_meeting_title_404(meetings_app):
    app, _repo, _bus = meetings_app
    client = TestClient(app)
    assert client.patch("/api/meetings/nope", json={"title": "x"}).status_code == 404


@pytest.mark.asyncio
async def test_patch_meeting_title_whitespace_only_422_and_no_write(meetings_app):
    """I5: '   ' passes pydantic's min_length=1 but is an empty title after
    strip — must be rejected without touching the row."""
    app, repo, bus = meetings_app
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title="Kept Title", title_source="auto")

    client = TestClient(app)
    resp = client.patch(f"/api/meetings/{mid}", json={"title": "   "})
    assert resp.status_code == 422

    m = await repo.get_meeting(mid)
    assert m.title == "Kept Title"
    assert m.title_source == "auto"
    assert not any(e["type"] == "meeting.renamed" for e in bus.events)
