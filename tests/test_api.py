"""API integration tests using httpx AsyncClient."""

import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.api.auth import verify_token
from src.api.routes import meetings as meetings_routes
from src.api.routes import status as status_routes
from src.db.database import Database
from src.db.repository import MeetingRepository

# A known test token for auth.
TEST_TOKEN = "test-token-for-api-tests"


def _make_app(repo: MeetingRepository) -> FastAPI:
    """Build a minimal FastAPI app for testing."""
    app = FastAPI()

    status_routes.init(
        get_daemon_state=lambda: "idle",
        get_active_meeting=lambda: None,
    )
    meetings_routes.init(repo)

    auth_deps = [Depends(verify_token)]
    app.include_router(status_routes.router, dependencies=auth_deps)
    app.include_router(meetings_routes.router, dependencies=auth_deps)
    return app


@pytest.fixture
async def client(db: Database):
    """Provide a TestClient backed by a test database."""
    import src.api.auth as auth_mod

    # Override the auth token for tests.
    original_token = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN

    repo = MeetingRepository(db)
    app = _make_app(repo)

    with TestClient(app) as c:
        yield c, repo

    # Restore original token.
    auth_mod._auth_token = original_token


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.mark.asyncio
async def test_health_no_auth(client):
    c, _ = client
    resp = c.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_status_requires_auth(client):
    c, _ = client
    resp = c.get("/api/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_status_with_auth(client):
    c, _ = client
    resp = c.get("/api/status", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "idle"


@pytest.mark.asyncio
async def test_invalid_token(client):
    c, _ = client
    resp = c.get("/api/status", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_meetings_crud(client):
    c, repo = client

    # Empty list initially
    resp = c.get("/api/meetings", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # Create a meeting via the repo
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Test Meeting", status="complete")

    # List should have one meeting
    resp = c.get("/api/meetings", headers=_auth_headers())
    data = resp.json()
    assert data["total"] == 1
    assert data["meetings"][0]["title"] == "Test Meeting"

    # Get single meeting
    resp = c.get(f"/api/meetings/{mid}", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["title"] == "Test Meeting"

    # Delete meeting
    resp = c.delete(f"/api/meetings/{mid}", headers=_auth_headers())
    assert resp.status_code == 200

    # Verify deleted
    resp = c.get(f"/api/meetings/{mid}", headers=_auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_meeting_not_found(client):
    c, _ = client
    resp = c.get("/api/meetings/nonexistent", headers=_auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_meetings_pagination(client):
    c, repo = client
    now = time.time()
    for i in range(5):
        await repo.create_meeting(started_at=now + i)

    resp = c.get("/api/meetings?limit=2&offset=0", headers=_auth_headers())
    data = resp.json()
    assert len(data["meetings"]) == 2
    assert data["total"] == 5

    resp = c.get("/api/meetings?limit=2&offset=4", headers=_auth_headers())
    data = resp.json()
    assert len(data["meetings"]) == 1
