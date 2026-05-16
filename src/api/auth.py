"""
Authentication for the Context Recall API.

Generates a shared secret token on first run and validates it
on incoming requests. Since the API binds to 127.0.0.1 only,
this prevents other local applications from controlling the daemon.
"""

import hmac
import logging
import os
import secrets
import threading

from fastapi import HTTPException, Request

from src.utils.paths import app_support_dir, auth_token_path

logger = logging.getLogger("contextrecall.auth")

TOKEN_DIR = app_support_dir()
TOKEN_PATH = auth_token_path()


def get_or_create_token() -> str:
    """Read the auth token from disk, or generate one on first run."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    _write_token_atomic(token)
    logger.info("Generated new auth token at %s", TOKEN_PATH)
    return token


def _write_token_atomic(token: str) -> None:
    """Write the token to disk atomically with mode 0o600.

    Writes to a sibling ``.tmp`` file (created with restrictive
    permissions), then ``os.replace`` to the final path so a partial
    write can never leave a half-written token file in place. ``chmod``
    is applied explicitly because the caller's umask may strip bits
    from the ``os.open`` mode argument.
    """
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_PATH.with_suffix(TOKEN_PATH.suffix + ".tmp")
    fd = os.open(
        str(tmp_path),
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, TOKEN_PATH)


# Module-level token loaded once at import.
_auth_token: str | None = None
_auth_token_lock = threading.Lock()


def _get_token() -> str:
    """Return the cached auth token, lazily initialising under a lock.

    The double-checked pattern keeps the hot path lock-free once the
    token has been loaded, while still preventing two requests from
    racing to read/generate the on-disk token concurrently.
    """
    global _auth_token
    if _auth_token is None:
        with _auth_token_lock:
            if _auth_token is None:
                _auth_token = get_or_create_token()
    return _auth_token


def rotate_token() -> str:
    """Generate a new auth token, persist it atomically, update the cache.

    Returns the new token value. The on-disk file is written via a
    ``.tmp`` + ``os.replace`` sequence so a crash mid-write cannot leave
    the daemon with an unreadable token file.
    """
    global _auth_token
    new_token = secrets.token_urlsafe(32)
    _write_token_atomic(new_token)
    with _auth_token_lock:
        _auth_token = new_token
    logger.info("Rotated auth token")
    return new_token


async def verify_token(request: Request) -> None:
    """FastAPI dependency that checks the Bearer token.

    Allows unauthenticated access to /api/health for connectivity checks.
    """
    if request.url.path == "/api/health":
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")

    token = auth_header.removeprefix("Bearer ").strip()
    expected = _get_token()
    # Short-circuit on length before constant-time compare to avoid
    # leaking the length of the secret via response timing.
    if len(token) != len(expected):
        raise HTTPException(status_code=403, detail="Invalid auth token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid auth token")
