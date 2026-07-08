"""Tests for src/api/routes/trackers.py — tracker CRUD + hits."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import trackers as trackers_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.trackers.repository import TrackerRepository

TEST_TOKEN = "test-token-for-trackers-tests"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "trackers_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    tracker_repo = TrackerRepository(db)
    trackers_routes.init(repo, tracker_repo)

    app = FastAPI()
    app.include_router(trackers_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "tracker_repo": tracker_repo}
    await db.close()


@pytest.mark.asyncio
async def test_tracker_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/trackers",
            headers=_auth_headers(),
            json={"name": "Pricing", "keywords": ["pricing", " cost ", "x"]},
        )
        assert created.status_code == 201
        tracker = created.json()
        assert tracker["keywords"] == ["pricing", "cost"]  # cleaned
        tracker_id = tracker["id"]

        patched = c.patch(
            f"/api/trackers/{tracker_id}",
            headers=_auth_headers(),
            json={"enabled": False},
        )
        assert patched.json()["enabled"] is False

        assert c.delete(f"/api/trackers/{tracker_id}", headers=_auth_headers()).status_code == 200
        assert c.get("/api/trackers", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_tracker_requires_usable_keywords(api):
    with TestClient(api["app"]) as c:
        resp = c.post(
            "/api/trackers",
            headers=_auth_headers(),
            json={"name": "Bad", "keywords": ["x", " "]},
        )
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_hits_endpoints(api):
    repo = api["repo"]
    tracker_repo = api["tracker_repo"]
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(meeting_id, title="Budget sync")
    tracker_id = await tracker_repo.create(name="T", keywords=["budget"])
    await tracker_repo.replace_hits_for_meeting(
        meeting_id,
        [
            {
                "tracker_id": tracker_id,
                "segment_index": 1,
                "matched_keyword": "budget",
                "matched_text": "the budget is fine",
                "start_time": 5.0,
            }
        ],
    )

    with TestClient(api["app"]) as c:
        by_tracker = c.get(f"/api/trackers/{tracker_id}/hits", headers=_auth_headers()).json()
        by_meeting = c.get(
            f"/api/meetings/{meeting_id}/tracker-hits", headers=_auth_headers()
        ).json()

    assert by_tracker[0]["meeting_title"] == "Budget sync"
    assert by_meeting[0]["tracker_name"] == "T"
    assert by_meeting[0]["matched_keyword"] == "budget"


@pytest.mark.asyncio
async def test_hits_404s(api):
    with TestClient(api["app"]) as c:
        assert c.get("/api/trackers/nope/hits", headers=_auth_headers()).status_code == 404
        assert c.get("/api/meetings/nope/tracker-hits", headers=_auth_headers()).status_code == 404
