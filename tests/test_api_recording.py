"""Tests for src/api/routes/recording.py — manual recording control."""

import asyncio

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import recording as recording_routes

TEST_TOKEN = "test-token-for-recording-tests"


def _make_recording_app() -> FastAPI:
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(recording_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_recording_globals():
    """Reset module-level globals before and after each test."""
    recording_routes._start_recording = None
    recording_routes._stop_recording = None
    recording_routes._stop_recording_deferred = None
    recording_routes._is_recording = None
    yield
    recording_routes._start_recording = None
    recording_routes._stop_recording = None
    recording_routes._stop_recording_deferred = None
    recording_routes._is_recording = None


@pytest.fixture
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def test_start_recording_success(_patch_auth):
    started = []
    recording_routes.init(
        start_recording=lambda: started.append(True),
        stop_recording=lambda: None,
        stop_deferred=lambda: "",
        is_recording=lambda: False,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/start", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "recording"
        assert "started_at" in data
        assert len(started) == 1


def test_start_recording_already_recording(_patch_auth):
    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=lambda: "",
        is_recording=lambda: True,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/start", headers=_auth_headers())
        assert resp.status_code == 409


def test_start_recording_controls_not_set(_patch_auth):
    # Don't call init — controls remain None.
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/start", headers=_auth_headers())
        assert resp.status_code == 503


def test_stop_recording_success(_patch_auth):
    stopped = []
    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: stopped.append(True),
        stop_deferred=lambda: "",
        is_recording=lambda: True,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopping"


def test_stop_recording_not_recording(_patch_auth):
    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=lambda: "",
        is_recording=lambda: False,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop", headers=_auth_headers())
        assert resp.status_code == 409


def test_stop_recording_controls_not_set(_patch_auth):
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop", headers=_auth_headers())
        assert resp.status_code == 503


def test_stop_recording_deferred_success(_patch_auth):
    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=lambda: "meeting-abc",
        is_recording=lambda: True,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop?defer=true", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == {"status": "deferred", "meeting_id": "meeting-abc"}


def test_stop_recording_deferred_failure_returns_500(_patch_auth):
    def boom():
        raise RuntimeError("No audio file produced")

    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=boom,
        is_recording=lambda: True,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop?defer=true", headers=_auth_headers())
        assert resp.status_code == 500


def test_stop_recording_deferred_not_available(_patch_auth):
    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=None,
        is_recording=lambda: True,
    )
    app = _make_recording_app()
    with TestClient(app) as c:
        resp = c.post("/api/record/stop?defer=true", headers=_auth_headers())
        assert resp.status_code == 503


def test_stop_recording_deferred_runs_off_the_event_loop(_patch_auth):
    """Regression test for the 'permanently pending meeting' bug.

    The real deferred-stop callback (api_stop_recording_deferred →
    _persist_audio) blocks while waiting for a coroutine it schedules
    onto the API server's own event loop. If the route executes the
    callback ON that loop, the wait can never complete: the create-meeting
    future times out, the failure is swallowed, and the meeting row is
    later created without an audio_path — unprocessable from the UI.

    This test mirrors that exact shape: the callback schedules a coroutine
    on the app's running loop and waits on it. It only succeeds when the
    route runs the callback off the loop thread.
    """
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    def stop_deferred() -> str:
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop_holder["loop"])
        future.result(timeout=2)
        return "meeting-relinked"

    recording_routes.init(
        start_recording=lambda: None,
        stop_recording=lambda: None,
        stop_deferred=stop_deferred,
        is_recording=lambda: True,
    )
    app = _make_recording_app()

    @app.get("/test/capture-loop")
    async def capture_loop():
        loop_holder["loop"] = asyncio.get_running_loop()
        return {"ok": True}

    with TestClient(app) as c:
        assert c.get("/test/capture-loop").status_code == 200
        resp = c.post("/api/record/stop?defer=true", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json() == {"status": "deferred", "meeting_id": "meeting-relinked"}
