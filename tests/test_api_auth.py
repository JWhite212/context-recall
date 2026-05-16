"""Tests for src/api/auth.py — token generation and verification."""

import os
import stat

import pytest
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


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


# ---- Tests 1-3: Token verification via HTTP ----


def test_empty_bearer_token_returns_403():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get("/api/status", headers={"Authorization": "Bearer "})
        assert resp.status_code == 403


def test_missing_bearer_prefix_returns_401():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get("/api/status", headers={"Authorization": "Token abc"})
        assert resp.status_code == 401


def test_token_in_query_param_not_accepted():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get(f"/api/status?token={TEST_TOKEN}")
        assert resp.status_code == 401


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


# ---- Tests 7-9: Additional auth edge cases ----


def test_whitespace_padded_token_accepted():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get(
            "/api/status",
            headers={"Authorization": f"Bearer   {TEST_TOKEN}  "},
        )
        assert resp.status_code == 200


def test_no_authorization_header_returns_401():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get("/api/status")
        assert resp.status_code == 401


def test_valid_token_accepted():
    app = _make_auth_app()
    with TestClient(app) as c:
        resp = c.get("/api/status", headers=_auth_headers())
        assert resp.status_code == 200


def test_token_length_mismatch_returns_403():
    """A token of the wrong length is rejected before the constant-time compare.

    This guards against length-leak timing attacks: the verifier must
    short-circuit on len() before invoking hmac.compare_digest.
    """
    app = _make_auth_app()
    with TestClient(app) as c:
        too_short = c.get("/api/status", headers=_auth_headers("x"))
        too_long = c.get(
            "/api/status",
            headers=_auth_headers(TEST_TOKEN + "extra-suffix-bytes"),
        )
        assert too_short.status_code == 403
        assert too_long.status_code == 403


def test_get_token_thread_safe_lazy_init(tmp_path, monkeypatch):
    """Concurrent _get_token() calls must converge on a single value.

    Without a lock around the lazy init, two threads could both observe
    ``_auth_token is None`` and generate competing tokens.
    """
    import threading

    monkeypatch.setattr(auth_mod, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(auth_mod, "TOKEN_PATH", tmp_path / "auth_token")
    monkeypatch.setattr(auth_mod, "_auth_token", None)

    seen: list[str] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        seen.append(auth_mod._get_token())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen)) == 1, f"Race condition produced multiple tokens: {set(seen)}"
    assert (tmp_path / "auth_token").read_text().strip() == seen[0]
