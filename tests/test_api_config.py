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
    config_path.write_text(
        yaml.dump(
            {
                "summarisation": {"anthropic_api_key": "sk-secret-key-12345"},
                "notion": {"api_key": "ntn_secret_abcdef"},
            }
        )
    )
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
            "detection",
            "audio",
            "transcription",
            "summarisation",
            "diarisation",
            "markdown",
            "notion",
            "logging",
            "api",
            "retention",
        ]:
            assert section in data, f"Missing section: {section}"


def test_put_config_preserves_masked_secret(tmp_path):
    config_path = tmp_path / "config.yaml"
    original_key = "sk-real-secret-key"
    config_path.write_text(
        yaml.dump(
            {
                "summarisation": {"anthropic_api_key": original_key},
            }
        )
    )
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
    config_path.write_text(
        yaml.dump(
            {
                "summarisation": {"anthropic_api_key": "old-key"},
            }
        )
    )
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
    config_path.write_text(
        yaml.dump(
            {
                "detection": {
                    "poll_interval_seconds": 3,
                    "min_meeting_duration_seconds": 30,
                },
            }
        )
    )
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


def test_empty_secret_not_masked(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "summarisation": {"anthropic_api_key": ""},
            }
        )
    )
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.get("/api/config", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["summarisation"]["anthropic_api_key"] == ""


def test_put_config_empty_body(tmp_path):
    config_path = tmp_path / "config.yaml"
    original_content = yaml.dump({"detection": {"poll_interval_seconds": 3}})
    config_path.write_text(original_content)
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.put("/api/config", headers=_auth_headers(), json={})
        assert resp.status_code == 200

    saved = yaml.safe_load(config_path.read_text())
    assert saved["detection"]["poll_interval_seconds"] == 3


def test_put_config_accepts_full_get_roundtrip(tmp_path):
    """The UI saves by PUTting the entire GET body back — every section
    GET returns must be accepted by PUT. Regression: action_items, series,
    analytics, and prep were missing from ConfigUpdateBody, so every save
    failed with 4x 'Extra inputs are not permitted'."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        fetched = c.get("/api/config", headers=_auth_headers()).json()
        resp = c.put("/api/config", headers=_auth_headers(), json=fetched)
        assert resp.status_code == 200, resp.text


def test_config_update_body_covers_all_appconfig_sections():
    """Drift guard: PUT must accept exactly the sections AppConfig defines."""
    import dataclasses

    from src.utils.config import AppConfig

    body_fields = set(config_routes.ConfigUpdateBody.model_fields)
    appconfig_fields = {f.name for f in dataclasses.fields(AppConfig)}
    assert body_fields == appconfig_fields


def test_get_config_reflects_yaml_for_intelligence_sections(tmp_path):
    """GET must return the YAML values, not dataclass defaults, for the
    calendar / action_items / series / analytics / prep sections."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "calendar": {"enabled": True},
                "action_items": {"auto_extract": False},
                "series": {"min_meetings_for_series": 7},
                "analytics": {"refresh_interval_hours": 12},
                "prep": {"lead_time_minutes": 45},
            }
        )
    )
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        data = c.get("/api/config", headers=_auth_headers()).json()
        assert data["calendar"]["enabled"] is True
        assert data["action_items"]["auto_extract"] is False
        assert data["series"]["min_meetings_for_series"] == 7
        assert data["analytics"]["refresh_interval_hours"] == 12
        assert data["prep"]["lead_time_minutes"] == 45


def test_put_config_roundtrip_persists_intelligence_sections(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("")
    app = _make_config_app(config_path)
    with TestClient(app) as c:
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"prep": {"lead_time_minutes": 30}, "analytics": {"rolling_window_weeks": 8}},
        )
        assert resp.status_code == 200, resp.text
    saved = yaml.safe_load(config_path.read_text())
    assert saved["prep"]["lead_time_minutes"] == 30
    assert saved["analytics"]["rolling_window_weeks"] == 8


def test_put_config_no_config_path_returns_500(tmp_path, monkeypatch):
    app = _make_config_app(tmp_path / "config.yaml")
    # Override _config_path to None after app creation to simulate uninitialised state.
    monkeypatch.setattr(config_routes, "_config_path", None)
    with TestClient(app) as c:
        resp = c.put(
            "/api/config",
            headers=_auth_headers(),
            json={"detection": {"poll_interval_seconds": 5}},
        )
        assert resp.status_code == 500
