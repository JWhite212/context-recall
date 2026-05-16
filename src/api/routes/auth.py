"""
Auth-management endpoints.

Currently exposes only ``POST /api/auth/rotate`` which generates a new
shared secret, replaces the on-disk token atomically, and updates the
in-process cache so subsequent requests must use the new value.
"""

from fastapi import APIRouter

from src.api.auth import rotate_token

router = APIRouter()


@router.post("/api/auth/rotate", summary="Rotate the API auth token")
async def rotate() -> dict[str, str]:
    """Generate a new auth token and return it.

    The caller must already hold a valid token (the route is registered
    behind ``verify_token`` like the rest of the API). The previous
    token is invalidated as soon as this endpoint returns.
    """
    new_token = rotate_token()
    return {"token": new_token}
