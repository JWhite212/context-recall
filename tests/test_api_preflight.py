"""Tests for src/api/routes/preflight.py — /api/preflight endpoint."""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import preflight as preflight_routes
from src.audio_preflight import PreflightReport
from src.system_audio import BlackHoleSystemCapture, ScreenCaptureKitSystemCapture
from src.utils.config import AudioConfig

TEST_TOKEN = "test-token-for-preflight-tests"


def _make_app() -> FastAPI:
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(preflight_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def test_preflight_endpoint_requires_auth():
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/preflight")
        assert resp.status_code == 401


def test_preflight_endpoint_returns_report():
    fake_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
        warnings=[],
        errors=[],
    )
    app = _make_app()
    with TestClient(app) as c:
        with patch(
            "src.api.routes.preflight.run_preflight",
            return_value=fake_report,
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200
            data = resp.json()
            assert data["blackhole_present"] is True
            assert data["blackhole_input_candidates"] == ["BlackHole 2ch"]
            assert data["mic_openable"] is True
            assert data["microphone_permission_likely"] is True
            assert data["default_input_index"] == 0
            assert data["warnings"] == []
            assert data["errors"] == []


def test_preflight_endpoint_surfaces_errors_and_warnings():
    fake_report = PreflightReport(
        blackhole_present=False,
        blackhole_input_candidates=[],
        mic_openable=False,
        microphone_permission_likely=False,
        default_input_index=None,
        warnings=["Microphone permission likely denied."],
        errors=["BlackHole virtual audio driver is not installed."],
    )
    app = _make_app()
    with TestClient(app) as c:
        with patch(
            "src.api.routes.preflight.run_preflight",
            return_value=fake_report,
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200
            data = resp.json()
            assert data["blackhole_present"] is False
            assert "BlackHole" in data["errors"][0]
            assert "Microphone" in data["warnings"][0]


def test_preflight_endpoint_falls_back_when_config_fails():
    """A broken config.yaml shouldn't 500 the endpoint — fall back to
    AudioConfig defaults and still return a useful device report."""
    fake_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=False,
        microphone_permission_likely=False,
        default_input_index=None,
        warnings=[],
        errors=[],
    )
    app = _make_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        with (
            patch(
                "src.api.routes.preflight.load_config",
                side_effect=RuntimeError("config broken"),
            ),
            patch(
                "src.api.routes.preflight.run_preflight",
                return_value=fake_report,
            ),
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["blackhole_present"] is True


def test_preflight_reports_screen_recording_granted_for_sck_backend():
    """When the selected backend is ScreenCaptureKit, /api/preflight probes
    its preflight() and surfaces the result under screen_recording — the
    UI's mechanism for detecting Screen Recording TCC status."""
    fake_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
        warnings=[],
        errors=[],
    )
    fake_backend = ScreenCaptureKitSystemCapture(AudioConfig(), Path("/nonexistent/helper"))
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(ScreenCaptureKitSystemCapture, "preflight", return_value="granted"),
            patch.object(preflight_routes, "select_system_backend", return_value=fake_backend),
            patch.object(preflight_routes, "run_preflight", return_value=fake_report),
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200, resp.text
            assert resp.json()["screen_recording"] == "granted"


def test_preflight_degrades_to_unknown_when_backend_selection_raises():
    """If select_system_backend blows up (e.g. a broken helper resolve),
    the route must degrade screen_recording to "unknown" and still return
    200 — never 500 (T8)."""
    fake_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
        warnings=[],
        errors=[],
    )
    app = _make_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        with (
            patch.object(
                preflight_routes,
                "select_system_backend",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(preflight_routes, "run_preflight", return_value=fake_report),
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200, resp.text
            assert resp.json()["screen_recording"] == "unknown"


def test_preflight_reports_screen_recording_not_applicable_for_blackhole_backend():
    """When BlackHole is the selected backend (SCK not in use), screen_recording
    should be not_applicable rather than probing anything."""
    fake_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
        warnings=[],
        errors=[],
    )
    fake_backend = BlackHoleSystemCapture(AudioConfig())
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(preflight_routes, "select_system_backend", return_value=fake_backend),
            patch.object(preflight_routes, "run_preflight", return_value=fake_report),
        ):
            resp = c.get("/api/preflight", headers=_auth_headers())
            assert resp.status_code == 200, resp.text
            assert resp.json()["screen_recording"] == "not_applicable"
