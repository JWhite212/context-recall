"""Tests for src/api/routes/diagnostics.py - environment diagnostics endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import diagnostics as diagnostics_routes
from src.utils.config import AppConfig, AudioConfig, SummarisationConfig

TEST_TOKEN = "test-token-for-diagnostics-tests"

# Keys the endpoint must always return so the UI can rely on the contract.
EXPECTED_KEYS = {
    "platform",
    "apple_silicon",
    "blackhole_found",
    "blackhole_candidates",
    "configured_blackhole_device",
    "configured_blackhole_available",
    "microphone_available",
    "audio_output_devices",
    "ollama_reachable",
    "selected_ollama_model_available",
    "mlx_available",
    "whisper_model_cached",
    "database_accessible",
    "logs_dir_writable",
    "app_support_dir_writable",
    "ffmpeg_available",
}


def _make_app() -> FastAPI:
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(diagnostics_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _make_config(backend: str = "ollama", model: str = "qwen3:30b-a3b") -> AppConfig:
    cfg = AppConfig()
    cfg.summarisation = SummarisationConfig(backend=backend, ollama_model=model)
    return cfg


def test_diagnostics_returns_all_expected_keys():
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(diagnostics_routes, "load_config", return_value=_make_config()),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert EXPECTED_KEYS.issubset(data.keys())
    assert isinstance(data["selected_ollama_model_available"], bool)


def test_selected_ollama_model_available_true_when_model_present():
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=True),
            ),
            patch.object(diagnostics_routes, "load_config", return_value=_make_config()),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["ollama_reachable"] is True
    assert data["selected_ollama_model_available"] is True


def test_selected_ollama_model_available_false_when_model_missing():
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(diagnostics_routes, "load_config", return_value=_make_config()),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["ollama_reachable"] is True
    assert data["selected_ollama_model_available"] is False


def test_selected_ollama_model_available_false_when_backend_not_ollama():
    """When the summariser backend is not 'ollama' the field returns False
    without calling the helper."""
    helper_mock = AsyncMock(return_value=True)
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=helper_mock,
            ),
            patch.object(
                diagnostics_routes,
                "load_config",
                return_value=_make_config(backend="claude"),
            ),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["selected_ollama_model_available"] is False
    helper_mock.assert_not_awaited()


# ---- Helper-level tests for _selected_ollama_model_available ----


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *_, **__):
        self._response: _FakeResponse | None = None
        self._raise: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _url):
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response


def _patch_httpx_response(monkeypatch, response: _FakeResponse | None, raise_exc=None):
    def _factory(*args, **kwargs):
        client = _FakeAsyncClient()
        client._response = response
        client._raise = raise_exc
        return client

    monkeypatch.setattr(diagnostics_routes.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_helper_matches_exact_name(monkeypatch):
    payload = {"models": [{"name": "qwen3:30b-a3b"}, {"name": "llama3:8b"}]}
    _patch_httpx_response(monkeypatch, _FakeResponse(200, payload))
    assert await diagnostics_routes._selected_ollama_model_available("qwen3:30b-a3b") is True


@pytest.mark.asyncio
async def test_helper_matches_by_prefix_when_tag_differs(monkeypatch):
    payload = {"models": [{"name": "qwen3:7b"}]}
    _patch_httpx_response(monkeypatch, _FakeResponse(200, payload))
    assert await diagnostics_routes._selected_ollama_model_available("qwen3:30b-a3b") is True


@pytest.mark.asyncio
async def test_helper_returns_false_when_model_absent(monkeypatch):
    payload = {"models": [{"name": "llama3:8b"}]}
    _patch_httpx_response(monkeypatch, _FakeResponse(200, payload))
    assert await diagnostics_routes._selected_ollama_model_available("qwen3:30b-a3b") is False


@pytest.mark.asyncio
async def test_helper_returns_false_on_non_200(monkeypatch):
    _patch_httpx_response(monkeypatch, _FakeResponse(500, {}))
    assert await diagnostics_routes._selected_ollama_model_available("qwen3:30b-a3b") is False


@pytest.mark.asyncio
async def test_helper_returns_false_on_network_error(monkeypatch):
    _patch_httpx_response(monkeypatch, None, raise_exc=RuntimeError("boom"))
    assert await diagnostics_routes._selected_ollama_model_available("qwen3:30b-a3b") is False


@pytest.mark.asyncio
async def test_helper_returns_false_on_empty_model_name():
    assert await diagnostics_routes._selected_ollama_model_available("") is False


# ---------------------------------------------------------------------------
# Bug A3: surface BlackHole candidates so the user can fix a misconfig
# ---------------------------------------------------------------------------


def _make_config_with_audio(blackhole_name: str) -> AppConfig:
    cfg = AppConfig()
    cfg.audio = AudioConfig(blackhole_device_name=blackhole_name)
    cfg.summarisation = SummarisationConfig()
    return cfg


def _devices_with(*names_with_input_channels: tuple[str, int]) -> list[dict]:
    """Build a sounddevice-style device list: (name, max_input_channels).

    Devices with 0 input channels are output-only and must not appear in
    blackhole_candidates (the audio capture path needs INPUT)."""
    return [
        {
            "name": name,
            "max_input_channels": ch,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        }
        for name, ch in names_with_input_channels
    ]


def test_blackhole_candidates_lists_input_devices_only():
    """When multiple BlackHole inputs are installed, list all of them.
    Output-only entries with 'blackhole' in the name must be excluded —
    they aren't valid choices for blackhole_device_name."""
    devices = _devices_with(
        ("BlackHole 2ch", 2),
        ("BlackHole 16ch", 16),
        ("BlackHole 64ch (Output Only Renamed)", 0),  # output-only — exclude
        ("MacBook Pro Microphone", 1),
    )
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "load_config",
                return_value=_make_config_with_audio("BlackHole 2ch"),
            ),
            patch.object(
                diagnostics_routes.sd,
                "query_devices",
                return_value=devices,
            ),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    candidates = data["blackhole_candidates"]
    assert "BlackHole 2ch" in candidates
    assert "BlackHole 16ch" in candidates
    assert "BlackHole 64ch (Output Only Renamed)" not in candidates
    assert "MacBook Pro Microphone" not in candidates


def test_configured_blackhole_available_true_when_match_present():
    devices = _devices_with(
        ("BlackHole 2ch", 2),
        ("BlackHole 16ch", 16),
    )
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "load_config",
                return_value=_make_config_with_audio("BlackHole 2ch"),
            ),
            patch.object(
                diagnostics_routes.sd,
                "query_devices",
                return_value=devices,
            ),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured_blackhole_device"] == "BlackHole 2ch"
    assert data["configured_blackhole_available"] is True


def test_configured_blackhole_available_false_when_only_other_variant_installed():
    """The user installed BlackHole 16ch but config still says BlackHole 2ch
    — the UI needs to know both that a candidate exists AND that the
    configured name doesn't match it."""
    devices = _devices_with(
        ("BlackHole 16ch", 16),
        ("MacBook Pro Microphone", 1),
    )
    app = _make_app()
    with TestClient(app) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "load_config",
                return_value=_make_config_with_audio("BlackHole 2ch"),
            ),
            patch.object(
                diagnostics_routes.sd,
                "query_devices",
                return_value=devices,
            ),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["configured_blackhole_device"] == "BlackHole 2ch"
    assert data["configured_blackhole_available"] is False
    assert data["blackhole_candidates"] == ["BlackHole 16ch"]


def test_blackhole_fields_robust_to_query_devices_failure():
    """If sd.query_devices raises, the endpoint must still return the new
    fields with safe defaults rather than 500-ing."""
    app = _make_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        with (
            patch.object(
                diagnostics_routes,
                "_ollama_reachable",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "_selected_ollama_model_available",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                diagnostics_routes,
                "load_config",
                return_value=_make_config_with_audio("BlackHole 2ch"),
            ),
            patch.object(
                diagnostics_routes.sd,
                "query_devices",
                side_effect=RuntimeError("PortAudio not initialised"),
            ),
        ):
            resp = c.get("/api/diagnostics", headers=_auth_headers())

    assert resp.status_code == 200
    data = resp.json()
    assert data["blackhole_candidates"] == []
    assert data["configured_blackhole_available"] is False
    # configured name still echoes from config even when devices can't
    # be queried — so the UI can always tell the user what THEY have set.
    assert data["configured_blackhole_device"] == "BlackHole 2ch"
