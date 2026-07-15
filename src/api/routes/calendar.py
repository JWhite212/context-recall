"""Calendar endpoints: recorded-meeting range + upcoming-event import (Track B)."""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query

from src import calendar_permission
from src.utils.config import load_config

logger = logging.getLogger("contextrecall.api.calendar")

router = APIRouter()

# Injected at startup.
_repo = None  # MeetingRepository
_reader = None  # CalendarReader | None
_sync_job = None  # CalendarSyncJob | None


def init(repo, reader=None, sync_job=None) -> None:
    global _repo, _reader, _sync_job
    _repo = repo
    _reader = reader
    _sync_job = sync_job


_MAX_RANGE_SECONDS = 366 * 86400  # ~1 year


def _validate_range(start: float, end: float) -> None:
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    if (end - start) > _MAX_RANGE_SECONDS:
        raise HTTPException(status_code=422, detail="range must not exceed 366 days")


@router.get("/api/calendar/meetings", summary="List meetings for calendar view")
async def get_calendar_meetings(
    start: float = Query(..., description="Start unix timestamp (inclusive)"),
    end: float = Query(..., description="End unix timestamp (exclusive)"),
):
    """Return all meetings whose started_at falls within [start, end)."""
    _validate_range(start, end)
    meetings = await _repo.list_meetings_by_date_range(start, end)
    return {"meetings": [m.to_dict() for m in meetings], "count": len(meetings)}


@router.get("/api/calendar/events", summary="List upcoming calendar events")
async def get_calendar_events(
    start: float = Query(..., description="Start unix timestamp (inclusive)"),
    end: float = Query(..., description="End unix timestamp (exclusive)"),
):
    """Return meeting-like calendar events in [start, end), read live from EventKit.

    Never gate on ``_reader.available`` here: it only becomes True once
    list_events() performs its lazy EventKit init, so an up-front check
    would block that init forever. An unavailable reader returns [].
    """
    _validate_range(start, end)
    if _reader is None:
        return {"events": [], "count": 0}
    excluded = load_config().calendar.excluded_calendars
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _reader.list_events, start, end, excluded)
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@router.get("/api/calendar/calendars", summary="List available calendars")
async def get_calendars():
    """Return [{id, title}] for the Settings calendar-exclude UI."""
    if _reader is None:
        return {"calendars": []}
    loop = asyncio.get_running_loop()
    calendars = await loop.run_in_executor(None, _reader.list_calendars)
    return {"calendars": calendars}


@router.get("/api/calendar/permission", summary="Calendar TCC permission status")
async def get_calendar_permission():
    """Return this process's macOS Calendar permission status for the UI banner."""
    status = calendar_permission.authorization_status()
    return {"status": status, "granted": status == calendar_permission.AUTHORIZED}


@router.post("/api/calendar/sync", summary="Sync the calendar mirror now")
async def sync_calendar():
    """Mirror the rolling near-term window into calendar_events immediately."""
    if _reader is None or _sync_job is None:
        return {"synced": 0}
    config = load_config().calendar
    now = time.time()
    end = now + config.sync_horizon_days * 86400
    excluded = config.excluded_calendars
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _reader.list_events, now, end, excluded)
    synced = await _sync_job.apply(now, end, events)
    return {"synced": synced}
