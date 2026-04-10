"""Tests for src/api/routes/recording.py — manual recording control."""

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
    recording_routes._is_recording = None
    yield
    recording_routes._start_recording = None
    recording_routes._stop_recording = None
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
