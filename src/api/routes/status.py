"""
Status and health endpoints.
"""

import time

from fastapi import APIRouter

from src.api.schemas import HealthResponse, StatusResponse

router = APIRouter()


# These are set by the server at startup to reference shared state.
_get_daemon_state = None
_get_active_meeting = None


def init(get_daemon_state, get_active_meeting):
    """Inject state accessors from the main app."""
    global _get_daemon_state, _get_active_meeting
    _get_daemon_state = get_daemon_state
    _get_active_meeting = get_active_meeting


@router.get("/api/health", response_model=HealthResponse, summary="Health check")
async def health():
    return {"status": "ok", "timestamp": time.time()}


@router.get("/api/status", response_model=StatusResponse, summary="Daemon status")
async def status():
    state = _get_daemon_state() if _get_daemon_state else "unknown"
    active_meeting = _get_active_meeting() if _get_active_meeting else None

    result = {
        "state": state,
        "timestamp": time.time(),
    }

    if active_meeting:
        result["active_meeting"] = active_meeting

    return result
