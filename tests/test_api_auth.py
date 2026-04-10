"""Tests for src/api/auth.py — token generation and verification."""

import os
import stat

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import get_or_create_token, verify_token
from src.api.routes import status as status_routes

TEST_TOKEN = "test-token-for-auth-tests"


def _make_auth_app() -> FastAPI:
    """Build a minimal FastAPI app with a status route for auth testing."""
    app = FastAPI()
    status_routes.init(
        get_daemon_state=lambda: "idle",
        get_active_meeting=lambda: None,
    )
    auth_deps = [Depends(verify_token)]
    app.include_router(status_routes.router, dependencies=auth_deps)
    return app


def _auth_headers(token: str = TEST_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ---- Tests 1-3: Token verification via HTTP ----


def test_empty_bearer_token_returns_403():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = _make_auth_app()
        with TestClient(app) as c:
            resp = c.get("/api/status", headers={"Authorization": "Bearer "})
            assert resp.status_code == 403
    finally:
        auth_mod._auth_token = original


def test_missing_bearer_prefix_returns_401():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = _make_auth_app()
        with TestClient(app) as c:
            resp = c.get("/api/status", headers={"Authorization": "Token abc"})
            assert resp.status_code == 401
    finally:
        auth_mod._auth_token = original


def test_token_in_query_param_not_accepted():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    try:
        app = _make_auth_app()
        with TestClient(app) as c:
            resp = c.get(f"/api/status?token={TEST_TOKEN}")
            assert resp.status_code == 401
    finally:
        auth_mod._auth_token = original


# ---- Tests 4-6: Token generation / persistence ----


def test_get_or_create_token_generates_on_first_run(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")

    token = get_or_create_token()

    assert len(token) > 0
    assert (tmp_path / "auth_token").exists()
    assert (tmp_path / "auth_token").read_text().strip() == token


def test_get_or_create_token_reads_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")

    existing_token = "my-pre-existing-token"
    (tmp_path / "auth_token").write_text(existing_token)

    token = get_or_create_token()
    assert token == existing_token


def test_token_file_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")

    get_or_create_token()

    token_path = tmp_path / "auth_token"
    file_stat = os.stat(token_path)
    mode = stat.S_IMODE(file_stat.st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
