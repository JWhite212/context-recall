import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import meetings as meetings_routes
from src.calendar_events.repository import CalendarEventRepository
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-cal-link"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _body(uid="EK1:1000"):
    return {
        "event_uid": uid,
        "title": "Quick Catch-Up",
        "start_ts": 1000.0,
        "end_ts": 2800.0,
        "attendees": [{"name": "Jamie", "email": "j@x.com"}],
        "organizer": None,
        "join_url": "https://teams.microsoft.com/l/meetup-join/x",
        "meeting_id": "19:mtg@thread.v2",
        "calendar_name": "Work",
    }


class _Events:
    def __init__(self):
        self.type = None


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "cl_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    crepo = CalendarEventRepository(db)

    emitted = []

    class Bus:
        def emit(self, event):
            emitted.append(event)

    meetings_routes.init(repo, event_bus=Bus(), calendar_event_repo=crepo)
    app = FastAPI()
    app.include_router(meetings_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "repo": repo, "emitted": emitted}
    await db.close()


@pytest.mark.asyncio
async def test_link_and_unlink_roundtrip(api):
    mid = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    with TestClient(api["app"]) as c:
        r = c.put(f"/api/meetings/{mid}/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 200
        assert r.json()["calendar_event_uid"] == "EK1:1000"
        assert r.json()["calendar_event_title"] == "Quick Catch-Up"
        assert any(e["type"] == "meeting.calendar_link" for e in api["emitted"])

        r = c.delete(f"/api/meetings/{mid}/calendar-link", headers=_headers())
        assert r.status_code == 200
        assert r.json()["calendar_event_uid"] == ""


@pytest.mark.asyncio
async def test_link_unknown_meeting_404(api):
    with TestClient(api["app"]) as c:
        r = c.put("/api/meetings/nope/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_link_conflict_409(api):
    m1 = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    m2 = await api["repo"].create_meeting(started_at=1010.0, status="complete")
    with TestClient(api["app"]) as c:
        assert (
            c.put(f"/api/meetings/{m1}/calendar-link", json=_body(), headers=_headers()).status_code
            == 200
        )
        r = c.put(f"/api/meetings/{m2}/calendar-link", json=_body(), headers=_headers())
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_link_requires_event_uid_422(api):
    mid = await api["repo"].create_meeting(started_at=1005.0, status="complete")
    bad = _body()
    bad["event_uid"] = ""
    with TestClient(api["app"]) as c:
        r = c.put(f"/api/meetings/{mid}/calendar-link", json=bad, headers=_headers())
        assert r.status_code == 422
