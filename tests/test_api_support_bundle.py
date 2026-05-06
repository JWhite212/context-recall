"""Tests for src/api/routes/support_bundle.py — diagnostic support bundle."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import support_bundle as support_bundle_routes
from src.utils.config import (
    AppConfig,
    EmailChannelConfig,
    NotificationsConfig,
    NotionConfig,
    SummarisationConfig,
)

TEST_TOKEN = "test-token-for-support-bundle-tests"


def _make_app() -> FastAPI:
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(support_bundle_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _make_sensitive_config() -> AppConfig:
    cfg = AppConfig()
    cfg.summarisation = SummarisationConfig(
        backend="claude",
        anthropic_api_key="sk-fake-anthropic-secret",
    )
    cfg.notion = NotionConfig(
        enabled=True,
        api_key="secret-notion-token",
        database_id="db-id-not-secret",
    )
    cfg.notifications = NotificationsConfig(
        email=EmailChannelConfig(
            enabled=True,
            smtp_host="smtp.example.com",
            smtp_user="user@example.com",
            smtp_password="super-secret-password",
            from_address="from@example.com",
            to_address="to@example.com",
        ),
    )
    return cfg


def _fake_diagnostics_payload() -> dict:
    return {
        "platform": "macos",
        "apple_silicon": True,
        "blackhole_found": False,
        "microphone_available": True,
        "audio_output_devices": [],
        "ollama_reachable": False,
        "selected_ollama_model_available": False,
        "mlx_available": False,
        "whisper_model_cached": False,
        "database_accessible": True,
        "logs_dir_writable": True,
        "app_support_dir_writable": True,
        "ffmpeg_available": False,
        "active_profile": "test",
    }


@pytest.fixture
def fake_logs_dir(tmp_path, monkeypatch) -> Path:
    """Redirect paths.logs_dir() to a tmp dir with a sample log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "contextrecall.log").write_text("hello-from-test-log\n")
    monkeypatch.setattr(support_bundle_routes.paths, "logs_dir", lambda: log_dir)
    return log_dir


@pytest.fixture
def patched_helpers(monkeypatch, fake_logs_dir):
    """Patch heavy helpers to keep the test hermetic."""

    async def _fake_diag():
        return _fake_diagnostics_payload()

    monkeypatch.setattr(support_bundle_routes, "_diagnostics_handler", lambda: _fake_diag)
    monkeypatch.setattr(support_bundle_routes, "load_config", lambda: _make_sensitive_config())
    monkeypatch.setattr(
        support_bundle_routes,
        "_audio_devices_summary",
        lambda: [{"name": "Test Device", "max_input_channels": 1, "max_output_channels": 0}],
    )
    return None


def _open_zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content), mode="r")


def test_support_bundle_returns_zip(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "context-recall-support-bundle-" in resp.headers.get("content-disposition", "")

    zf = _open_zip(resp.content)
    names = set(zf.namelist())
    assert {
        "metadata.json",
        "config.redacted.json",
        "diagnostics.json",
        "audio_devices.json",
        "recent_logs.txt",
        "paths.json",
    }.issubset(names)


def test_metadata_json_parses_with_required_fields(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    metadata = json.loads(zf.read("metadata.json"))
    assert metadata["app_version"] == support_bundle_routes.APP_VERSION
    assert "platform" in metadata
    assert "profile" in metadata
    assert "generated_at" in metadata
    # Trailing 'Z' indicates UTC.
    assert metadata["generated_at"].endswith("Z")


def test_config_secrets_are_redacted(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    redacted = json.loads(zf.read("config.redacted.json"))

    placeholder = support_bundle_routes._REDACTED_PLACEHOLDER
    assert redacted["summarisation"]["anthropic_api_key"] == placeholder
    assert redacted["notion"]["api_key"] == placeholder
    assert redacted["notifications"]["email"]["smtp_password"] == placeholder

    # Non-sensitive fields are preserved verbatim.
    assert redacted["summarisation"]["backend"] == "claude"
    assert redacted["notion"]["enabled"] is True
    assert redacted["notion"]["database_id"] == "db-id-not-secret"

    # The literal secret strings must not survive anywhere in the dump.
    raw = json.dumps(redacted)
    assert "sk-fake-anthropic-secret" not in raw
    assert "secret-notion-token" not in raw
    assert "super-secret-password" not in raw


def test_diagnostics_json_round_trips(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    diag = json.loads(zf.read("diagnostics.json"))
    assert diag["platform"] == "macos"
    assert diag["active_profile"] == "test"


def test_recent_logs_includes_log_tail(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    logs = zf.read("recent_logs.txt").decode("utf-8")
    assert "hello-from-test-log" in logs
    assert "contextrecall.log" in logs


def test_audio_devices_json_present(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    devices = json.loads(zf.read("audio_devices.json"))
    assert isinstance(devices, list)
    assert devices[0]["name"] == "Test Device"


def test_paths_json_lists_paths_only(patched_helpers, monkeypatch, tmp_path):
    """paths.json must be string paths, not file contents."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    secret_audio = audio_dir / "meeting.wav"
    secret_audio.write_bytes(b"RIFFFAKEAUDIOcontent_should_never_appear")

    monkeypatch.setattr(support_bundle_routes.paths, "audio_dir", lambda: audio_dir)

    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle", headers=_auth_headers())

    zf = _open_zip(resp.content)
    paths_dump = json.loads(zf.read("paths.json"))
    assert paths_dump["audio_dir"] == str(audio_dir)
    assert "db_path" in paths_dump
    assert "logs_dir" in paths_dump

    # No member of the zip should be the audio file or contain its bytes.
    for name in zf.namelist():
        assert "meeting.wav" not in name
        contents = zf.read(name)
        assert b"FAKEAUDIOcontent_should_never_appear" not in contents


def test_redact_helper_handles_nested_structures():
    obj = {
        "outer_password": "shh",
        "nested": {"api_key": 42, "ok": "value"},
        "list_of_dicts": [{"secret": "x"}, {"name": "kept"}],
        "tuple_value": ("anthropic_api_key", {"token": "tok"}),
    }
    redacted = support_bundle_routes._redact(obj)
    placeholder = support_bundle_routes._REDACTED_PLACEHOLDER
    assert redacted["outer_password"] == placeholder
    assert redacted["nested"]["api_key"] == placeholder
    assert redacted["nested"]["ok"] == "value"
    assert redacted["list_of_dicts"][0]["secret"] == placeholder
    assert redacted["list_of_dicts"][1]["name"] == "kept"
    # The tuple element that was a dict gets its secret-bearing key redacted.
    assert redacted["tuple_value"][1]["token"] == placeholder


def test_redact_helper_preserves_non_secret_scalars():
    obj = {"name": "alice", "count": 3, "flag": True, "nothing": None}
    redacted = support_bundle_routes._redact(obj)
    assert redacted == obj


def test_endpoint_requires_auth(patched_helpers):
    app = _make_app()
    with TestClient(app) as c:
        resp = c.get("/api/support_bundle")
    assert resp.status_code == 401
