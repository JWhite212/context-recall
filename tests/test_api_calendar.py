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
    """Mirrors the real CalendarReader contract: reads on an unavailable
    reader return empty rather than raising, and `available` is a result
    of initialisation, not a precondition callers may gate on."""

    def __init__(self, events=None, calendars=None, available=True):
        self._events = events or []
        self._calendars = calendars or []
        self.available = available

    def list_events(self, start, end, excluded_calendars=None):
        if not self.available:
            return []
        return [e for e in self._events if start <= e.start_ts < end]

    def list_calendars(self):
        if not self.available:
            return []
        return self._calendars


class LazyFakeReader:
    """Models the real reader's lazy init: `available` only becomes True
    once list_events()/list_calendars() run _ensure_store()."""

    def __init__(self, events=None, calendars=None):
        self._events = events or []
        self._calendars = calendars or []
        self.available = False

    def list_events(self, start, end, excluded_calendars=None):
        self.available = True
        return [e for e in self._events if start <= e.start_ts < end]

    def list_calendars(self):
        self.available = True
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
async def test_routes_do_not_gate_on_available_before_lazy_init(api):
    """Regression (C1): the reader's `available` only flips True after
    list_events()/list_calendars() perform the lazy EventKit init. Gating
    the routes on `available` up front meant the reader was never called,
    so it could never initialise — events and calendars were permanently
    empty. The routes must call the reader and let IT decide."""
    lazy = LazyFakeReader(
        events=[_ev(uid="EK1:1000", start=1000.0)],
        calendars=[{"id": "c1", "title": "Work"}],
    )
    calendar_routes.init(None, lazy, None)
    assert lazy.available is False
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["count"] == 1
        assert r.json()["events"][0]["event_uid"] == "EK1:1000"

        r = c.get("/api/calendar/calendars", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["calendars"] == [{"id": "c1", "title": "Work"}]


@pytest.mark.asyncio
async def test_events_empty_when_reader_unavailable(api):
    api["reader"].available = False
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"events": [], "count": 0}


def test_calendar_permission_endpoint(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import calendar as calendar_routes

    monkeypatch.setattr("src.calendar_permission.authorization_status", lambda: "authorized")
    app = FastAPI()
    app.include_router(calendar_routes.router)
    client = TestClient(app)

    resp = client.get("/api/calendar/permission")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "authorized"
    assert body["granted"] is True


def test_request_calendar_access_endpoint(monkeypatch):
    """B6: macOS only lists an app under Privacy > Calendars once it has
    requested access (there is no manual add). This endpoint fires the request
    from the daemon so the app registers and the prompt appears."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import calendar as calendar_routes

    calls = {"n": 0}

    def _req(**_kw):
        calls["n"] += 1
        return True

    monkeypatch.setattr("src.calendar_permission.request_access", _req)
    monkeypatch.setattr("src.calendar_permission.authorization_status", lambda: "authorized")
    app = FastAPI()
    app.include_router(calendar_routes.router)
    client = TestClient(app)

    resp = client.post("/api/calendar/request")
    assert resp.status_code == 200
    assert resp.json() == {"status": "authorized", "granted": True}
    assert calls["n"] == 1


class _RecordingSync:
    """Mock sync job that records whether apply was called."""

    def __init__(self):
        self.apply_called = False

    async def apply(self, start, end, events):
        self.apply_called = True
        return 1


class _StaysUnavailableReader:
    """Reader that stays unavailable even after list_events runs."""

    def __init__(self):
        self.available = False

    def list_events(self, start, end, excluded_calendars=None):
        return []

    def list_calendars(self):
        return []


@pytest.mark.asyncio
async def test_post_sync_skips_apply_when_reader_unavailable():
    """Regression: when reader is unavailable (e.g. grant revoked),
    sync.apply must be skipped to avoid pruning the mirror. Route
    returns {synced: 0} without calling apply."""
    sync_job = _RecordingSync()
    reader = _StaysUnavailableReader()
    calendar_routes.init(None, reader, sync_job)
    app = FastAPI()
    app.include_router(calendar_routes.router, dependencies=[Depends(verify_token)])

    with TestClient(app) as c:
        r = c.post("/api/calendar/sync", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"synced": 0}
    assert sync_job.apply_called is False
