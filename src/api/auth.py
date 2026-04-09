"""
Authentication for the MeetingMind API.

Generates a shared secret token on first run and validates it
on incoming requests. Since the API binds to 127.0.0.1 only,
this prevents other local applications from controlling the daemon.
"""

import hmac
import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

logger = logging.getLogger("meetingmind.auth")

TOKEN_DIR = Path(os.path.expanduser("~/.config/meetingmind"))
TOKEN_PATH = TOKEN_DIR / "auth_token"


def get_or_create_token() -> str:
    """Read the auth token from disk, or generate one on first run."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token)
    # Restrict to owner-only read/write.
    TOKEN_PATH.chmod(0o600)
    logger.info("Generated new auth token at %s", TOKEN_PATH)
    return token


# Module-level token loaded once at import.
_auth_token: str | None = None


def _get_token() -> str:
    global _auth_token
    if _auth_token is None:
        _auth_token = get_or_create_token()
    return _auth_token


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
    if not hmac.compare_digest(token, _get_token()):
        raise HTTPException(status_code=403, detail="Invalid auth token")
