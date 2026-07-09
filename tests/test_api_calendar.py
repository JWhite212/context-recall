import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import calendar as calendar_routes
from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_events.sync import CalendarSyncJob
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-for-calendar"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


class FakeReader:
    def __init__(self, events=None, calendars=None, available=True):
        self._events = events or []
        self._calendars = calendars or []
        self.available = available

    def list_events(self, start, end, excluded_calendars=None):
        return [e for e in self._events if start <= e.start_ts < end]

    def list_calendars(self):
        return self._calendars


def _ev(uid="EK1:1000", start=1000.0):
    return CalendarEvent(
        event_uid=uid,
        title="Sync",
        start_ts=start,
        end_ts=start + 1800.0,
        attendees=[{"name": "A"}, {"name": "B"}],
        organizer=None,
        join_url="",
        meeting_id="",
        calendar_name="Work",
    )


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "cal_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    cal_repo = CalendarEventRepository(db)
    # Two events: one in past range for /events test, one in future for /sync test.
    future_ts = time.time() + 10 * 86400
    reader = FakeReader(
        events=[_ev(uid="EK1:1000", start=1000.0), _ev(uid="EK1:future", start=future_ts)],
        calendars=[{"id": "c1", "title": "Work"}],
    )
    sync_job = CalendarSyncJob(cal_repo)
    calendar_routes.init(repo, reader, sync_job)
    app = FastAPI()
    app.include_router(calendar_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "cal_repo": cal_repo, "reader": reader}
    await db.close()


@pytest.mark.asyncio
async def test_get_events_returns_events_in_range(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["events"][0]["event_uid"] == "EK1:1000"


@pytest.mark.asyncio
async def test_get_events_rejects_bad_range(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=5000&end=1000", headers=_auth_headers())
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_calendars(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/calendars", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["calendars"] == [{"id": "c1", "title": "Work"}]


@pytest.mark.asyncio
async def test_post_sync_mirrors_into_table(api):
    with TestClient(api["app"]) as c:
        r = c.post("/api/calendar/sync", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["synced"] == 1
    rows = await api["cal_repo"].list_by_range(0.0, 10**12)
    assert [row["event_uid"] for row in rows] == ["EK1:future"]


@pytest.mark.asyncio
async def test_events_empty_when_reader_unavailable(api):
    api["reader"].available = False
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"events": [], "count": 0}
