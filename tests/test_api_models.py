"""Tests for src/api/routes/models.py — model management endpoints."""

from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import models as models_routes

TEST_TOKEN = "test-token-for-models-tests"


def _make_app() -> FastAPI:
    models_routes.init(event_bus=None)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(models_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth_and_reset():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    original_downloads = models_routes._downloads.copy()
    models_routes._downloads = {}
    yield
    auth_mod._auth_token = original
    models_routes._downloads = original_downloads


def test_list_models_returns_all():
    app = _make_app()
    with TestClient(app) as c:
        with patch.object(models_routes, "_downloaded_repos", return_value=set()):
            resp = c.get("/api/models", headers=_auth_headers())
            assert resp.status_code == 200
            data = resp.json()
            model_names = {m["name"] for m in data["models"]}
            for name in models_routes.AVAILABLE_MODELS:
                assert name in model_names


def test_list_models_with_download_status():
    app = _make_app()
    with TestClient(app) as c:
        # Pretend one model is downloaded.
        downloaded = {"mlx-community/whisper-tiny"}
        with patch.object(models_routes, "_downloaded_repos", return_value=downloaded):
            resp = c.get("/api/models", headers=_auth_headers())
            assert resp.status_code == 200
            data = resp.json()
            for model in data["models"]:
                if model["name"] == "tiny":
                    assert model["status"] == "downloaded"
                else:
                    assert model["status"] == "not_downloaded"


def test_every_model_is_mlx_format():
    """The transcriber passes config.transcription.model_size straight to
    mlx_whisper as path_or_hf_repo — CTranslate2 repos (faster-whisper)
    can never be loaded. Production 2026-07-07: the UI downloaded the
    2.9 GB Systran/faster-whisper-large-v3 which no code path can use."""
    for name, info in models_routes.AVAILABLE_MODELS.items():
        assert info["repo"].startswith("mlx-community/"), (
            f"model {name!r} points at non-MLX repo {info['repo']!r}"
        )


def test_active_model_flagged_from_config(tmp_path):
    """The model whose repo matches transcription.model_size is marked
    active so the UI can show which download is actually in use."""
    import yaml

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump({"transcription": {"model_size": "mlx-community/whisper-large-v3-turbo"}})
    )
    models_routes.init(event_bus=None, config_path=config_path)
    app = _make_app_no_init()
    with TestClient(app) as c:
        with patch.object(models_routes, "_downloaded_repos", return_value=set()):
            resp = c.get("/api/models", headers=_auth_headers())
            assert resp.status_code == 200
            by_name = {m["name"]: m for m in resp.json()["models"]}
            assert by_name["large-v3-turbo"]["active"] is True
            inactive = [n for n, m in by_name.items() if not m["active"]]
            assert len(inactive) == len(by_name) - 1


def _make_app_no_init() -> FastAPI:
    """Router app without re-running init() (test configured it already)."""
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(models_routes.router, dependencies=auth_deps)
    return app


def test_download_unknown_model_404():
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/models/nonexistent/download", headers=_auth_headers())
        assert resp.status_code == 404


def test_download_already_downloaded():
    app = _make_app()
    with TestClient(app) as c:
        with patch.object(models_routes, "_is_downloaded", return_value=True):
            resp = c.post("/api/models/tiny/download", headers=_auth_headers())
            assert resp.status_code == 200
            assert resp.json()["status"] == "already_downloaded"


def test_download_already_in_progress():
    models_routes._downloads["small"] = {
        "status": "downloading",
        "error": None,
        "percent": 42,
    }
    app = _make_app()
    with TestClient(app) as c:
        with patch.object(models_routes, "_is_downloaded", return_value=False):
            resp = c.post("/api/models/small/download", headers=_auth_headers())
            assert resp.status_code == 200
            assert resp.json()["status"] == "already_downloading"


def test_download_starts_thread():
    app = _make_app()
    with TestClient(app) as c:
        # Mock _download_worker instead of threading.Thread — patching
        # threading.Thread globally breaks run_in_executor's thread pool,
        # causing a deadlock.
        with (
            patch.object(models_routes, "_downloaded_repos", return_value=set()),
            patch.object(models_routes, "_download_worker"),
        ):
            resp = c.post("/api/models/base/download", headers=_auth_headers())
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"
            # The download was registered in the module-level dict.
            assert "base" in models_routes._downloads
            assert models_routes._downloads["base"]["status"] == "downloading"
