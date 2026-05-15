"""Tests for src/api/routes/auth.py — token rotation endpoint."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import auth as auth_routes
from src.api.routes import status as status_routes

TEST_TOKEN = "test-token-for-rotate-tests"


def _make_app() -> FastAPI:
    """Build a FastAPI app exposing both /api/status and /api/auth/rotate."""
    app = FastAPI()
    status_routes.init(
        get_daemon_state=lambda: "idle",
        get_active_meeting=lambda: None,
    )
    auth_deps = [Depends(verify_token)]
    app.include_router(status_routes.router, dependencies=auth_deps)
    app.include_router(auth_routes.router, dependencies=auth_deps)
    return app


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """Redirect on-disk token storage and reset the module cache."""
    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")
    # Seed the cache to a known value matching TEST_TOKEN so the auth
    # dependency accepts the bearer header.
    (tmp_path / "auth_token").write_text(TEST_TOKEN)
    os.chmod(tmp_path / "auth_token", 0o600)

    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield tmp_path
    auth_mod._auth_token = original


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rotate_requires_auth(patched_paths):
    """Calling /api/auth/rotate without a token returns 401."""
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/rotate")
        assert resp.status_code == 401


def test_rotate_rejects_bad_token(patched_paths):
    """Calling /api/auth/rotate with the wrong token returns 403."""
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/rotate", headers=_headers("nope"))
        assert resp.status_code == 403


def test_rotate_returns_new_token_and_updates_cache(patched_paths):
    """Successful rotation returns a fresh token and invalidates the old one."""
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/rotate", headers=_headers(TEST_TOKEN))
        assert resp.status_code == 200
        payload = resp.json()
        assert "token" in payload
        new_token = payload["token"]
        assert new_token and new_token != TEST_TOKEN

        # The module cache must be updated so the previous token is rejected.
        assert auth_mod._auth_token == new_token

        # Old token no longer works.
        old = c.get("/api/status", headers=_headers(TEST_TOKEN))
        assert old.status_code == 403

        # New token works.
        good = c.get("/api/status", headers=_headers(new_token))
        assert good.status_code == 200


def test_rotated_token_persisted_with_mode_0600(patched_paths):
    """The rotated token is written to disk with mode 0o600."""
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/rotate", headers=_headers(TEST_TOKEN))
        assert resp.status_code == 200
        new_token = resp.json()["token"]

    token_path = patched_paths / "auth_token"
    assert token_path.read_text().strip() == new_token

    mode = stat.S_IMODE(os.stat(token_path).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_rotate_atomic_no_tmp_left_behind(patched_paths):
    """After a successful rotation there must be no stray .tmp file."""
    app = _make_app()
    with TestClient(app) as c:
        resp = c.post("/api/auth/rotate", headers=_headers(TEST_TOKEN))
        assert resp.status_code == 200

    leftovers = list(patched_paths.glob("*.tmp"))
    assert leftovers == []


def test_rotate_token_helper_directly(tmp_path, monkeypatch):
    """The rotate_token helper should generate, persist, and cache atomically."""
    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")

    original = auth_mod._auth_token
    auth_mod._auth_token = "starting-token"
    try:
        new_token = auth_mod.rotate_token()
        assert new_token != "starting-token"
        assert auth_mod._auth_token == new_token
        assert (tmp_path / "auth_token").read_text().strip() == new_token
        mode = stat.S_IMODE(os.stat(tmp_path / "auth_token").st_mode)
        assert mode == 0o600
    finally:
        auth_mod._auth_token = original
