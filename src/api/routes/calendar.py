"""
Calendar-optimised meeting query endpoint.

Returns meetings within a date range for calendar view rendering.
"""

import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger("meetingmind.api.calendar")

router = APIRouter()

# Injected at startup.
_repo = None


def init(repo):
    global _repo
    _repo = repo


_MAX_RANGE_SECONDS = 366 * 86400  # ~1 year


@router.get("/api/calendar/meetings", summary="List meetings for calendar view")
async def get_calendar_meetings(
    start: float = Query(..., description="Start unix timestamp (inclusive)"),
    end: float = Query(..., description="End unix timestamp (exclusive)"),
):
    """Return all meetings whose started_at falls within [start, end)."""
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    if (end - start) > _MAX_RANGE_SECONDS:
        raise HTTPException(status_code=422, detail="range must not exceed 366 days")

    meetings = await _repo.list_meetings_by_date_range(start, end)
    return {
        "meetings": [m.to_dict() for m in meetings],
        "count": len(meetings),
    }
