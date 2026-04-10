"""Tests for src/api/routes/config.py — configuration read/write."""

import pytest
import yaml
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import config as config_routes

TEST_TOKEN = "test-token-for-config-tests"


def _make_config_app(config_path) -> FastAPI:
    config_routes.init(config_path)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(config_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


@pytest.fixture(autouse=True)
def _reset_config_path():
    original = config_routes._config_path
    yield
    config_routes._config_path = original


def test_get_config_masks_api_keys(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "summarisation": {"anthropic_api_key": "sk-secret-key-12345"},
        "notion": {"api_key": "ntn_secret_abcdef"},
    }))
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.get("/api/config", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["summarisation"]["anthropic_api_key"] == "••••••••"
        assert data["notion"]["api_key"] == "••••••••"


def test_get_config_returns_full_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.get("/api/config", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        # All top-level sections should be present.
        for section in [
            "detection", "audio", "transcription", "summarisation",
            "diarisation", "markdown", "notion", "logging", "api", "retention",
        ]:
            assert section in data, f"Missing section: {section}"


def test_put_config_preserves_masked_secret(tmp_path):
    config_path = tmp_path / "config.yaml"
    original_key = "sk-real-secret-key"
    config_path.write_text(yaml.dump({
        "summarisation": {"anthropic_api_key": original_key},
    }))
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        # PUT with the mask value — should preserve the original key.
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"summarisation": {"anthropic_api_key": "••••••••"}},
        )
        assert resp.status_code == 200

    # Verify the file still has the original key.
    saved = yaml.safe_load(config_path.read_text())
    assert saved["summarisation"]["anthropic_api_key"] == original_key


def test_put_config_updates_real_value(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "summarisation": {"anthropic_api_key": "old-key"},
    }))
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"summarisation": {"anthropic_api_key": "new-key-12345"}},
        )
        assert resp.status_code == 200

    saved = yaml.safe_load(config_path.read_text())
    assert saved["summarisation"]["anthropic_api_key"] == "new-key-12345"


def test_put_config_rejects_unknown_top_level(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"unknown_section": {"key": "value"}},
        )
        assert resp.status_code == 422


def test_deep_merge_nested_dicts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "detection": {
            "poll_interval_seconds": 3,
            "min_meeting_duration_seconds": 30,
        },
    }))
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        # Update only one nested field.
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"detection": {"poll_interval_seconds": 5}},
        )
        assert resp.status_code == 200

    saved = yaml.safe_load(config_path.read_text())
    # The updated field should change.
    assert saved["detection"]["poll_interval_seconds"] == 5
    # The sibling field should be preserved.
    assert saved["detection"]["min_meeting_duration_seconds"] == 30
