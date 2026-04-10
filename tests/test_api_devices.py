"""Tests for src/api/routes/devices.py — audio device listing."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import devices as devices_routes

TEST_TOKEN = "test-token-for-devices-tests"


def _make_app() -> FastAPI:
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(devices_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def test_list_devices_returns_inputs_only():
    mock_devices = [
        {"name": "Built-in Mic", "max_input_channels": 2,
         "max_output_channels": 0, "default_samplerate": 44100.0},
        {"name": "Speakers", "max_input_channels": 0,
         "max_output_channels": 2, "default_samplerate": 44100.0},
        {"name": "BlackHole 2ch", "max_input_channels": 2,
         "max_output_channels": 2, "default_samplerate": 48000.0},
    ]
    mock_default = MagicMock()
    mock_default.device = [0, 1]

    app = _make_app()
    with TestClient(app) as c:
        with patch("src.api.routes.devices.sd.query_devices", return_value=mock_devices):
            with patch("src.api.routes.devices.sd.default", mock_default):
                resp = c.get("/api/devices", headers=_auth_headers())
                assert resp.status_code == 200
                data = resp.json()
                # Only devices with max_input_channels > 0.
                assert len(data["devices"]) == 2
                names = [d["name"] for d in data["devices"]]
                assert "Built-in Mic" in names
                assert "BlackHole 2ch" in names
                assert "Speakers" not in names


def test_list_devices_marks_default():
    mock_devices = [
        {"name": "USB Mic", "max_input_channels": 1,
         "max_output_channels": 0, "default_samplerate": 48000.0},
        {"name": "Built-in Mic", "max_input_channels": 2,
         "max_output_channels": 0, "default_samplerate": 44100.0},
    ]
    mock_default = MagicMock()
    mock_default.device = [1, 0]  # Default input is index 1.

    app = _make_app()
    with TestClient(app) as c:
        with patch("src.api.routes.devices.sd.query_devices", return_value=mock_devices):
            with patch("src.api.routes.devices.sd.default", mock_default):
                resp = c.get("/api/devices", headers=_auth_headers())
                assert resp.status_code == 200
                data = resp.json()
                for dev in data["devices"]:
                    if dev["name"] == "Built-in Mic":
                        assert dev["is_default"] is True
                    else:
                        assert dev["is_default"] is False


def test_list_devices_empty():
    mock_devices = [
        {"name": "Speakers", "max_input_channels": 0,
         "max_output_channels": 2, "default_samplerate": 44100.0},
    ]
    mock_default = MagicMock()
    mock_default.device = [0, 0]

    app = _make_app()
    with TestClient(app) as c:
        with patch("src.api.routes.devices.sd.query_devices", return_value=mock_devices):
            with patch("src.api.routes.devices.sd.default", mock_default):
                resp = c.get("/api/devices", headers=_auth_headers())
                assert resp.status_code == 200
                data = resp.json()
                assert data["devices"] == []
