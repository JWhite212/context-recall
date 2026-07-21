"""
Meeting history CRUD endpoints.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.schemas import DeleteResponse, MeetingListResponse, MeetingStatsResponse
from src.utils.config import load_config
from src.utils.paths import audio_dir as default_audio_dir

logger = logging.getLogger("contextrecall.api.meetings")

router = APIRouter()

# Injected at startup.
_repo = None
_event_bus = None
_calendar_event_repo = None


def init(repo, event_bus=None, calendar_event_repo=None):
    global _repo, _event_bus, _calendar_event_repo
    _repo = repo
    _event_bus = event_bus
    _calendar_event_repo = calendar_event_repo


class MergeMeetingsRequest(BaseModel):
    meeting_ids: list[str] = Field(min_length=2, max_length=50)


class SetLabelRequest(BaseModel):
    label: str = Field(default="", max_length=200)


class SetTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)


class RenameMeetingRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class CalendarLinkAttendee(BaseModel):
    name: str = ""
    email: str = ""


class CalendarLinkRequest(BaseModel):
    event_uid: str = Field(min_length=1, max_length=512)
    title: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    attendees: list[CalendarLinkAttendee] = Field(default_factory=list, max_length=200)
    organizer: CalendarLinkAttendee | None = None
    join_url: str = ""
    meeting_id: str = ""
    calendar_name: str = ""


@router.get("/api/meetings", response_model=MeetingListResponse, summary="List meetings")
async def list_meetings(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    q: str | None = Query(None),
    tag: str | None = Query(None),
    sort: str | None = Query(None),
    client_id: str | None = Query(None),
    project_id: str | None = Query(None),
):
    if q:
        # FTS has its own ranking — ignore sort param when searching.
        meetings = await _repo.search_meetings(q, limit=limit)
    else:
        meetings = await _repo.list_meetings(
            limit=limit,
            offset=offset,
            status=status,
            tag=tag,
            sort=sort,
            client_id=client_id,
            project_id=project_id,
        )

    total = await _repo.count_meetings(
        status=status, tag=tag, client_id=client_id, project_id=project_id
    )

    return {
        "meetings": [m.to_dict() for m in meetings],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --- Routes below MUST be registered before /api/meetings/{meeting_id} ---


@router.post("/api/meetings/merge", summary="Merge multiple meetings into one")
async def merge_meetings(body: MergeMeetingsRequest):
    meeting_ids = body.meeting_ids

    # Fetch all meetings, ordered by started_at.
    meetings = []
    for mid in meeting_ids:
        m = await _repo.get_meeting(mid)
        if not m:
            raise HTTPException(status_code=404, detail=f"Meeting {mid} not found")
        if not m.transcript_json:
            raise HTTPException(status_code=400, detail=f"Meeting {mid} has no transcript")
        meetings.append(m)

    meetings.sort(key=lambda m: m.started_at)

    # Merge transcripts.
    merged_segments = []
    for m in meetings:
        transcript_data = json.loads(m.transcript_json)
        segments = transcript_data.get("segments", [])
        merged_segments.extend(segments)

    # Calculate merged metadata.
    earliest = meetings[0]
    latest = meetings[-1]
    total_duration = sum(m.duration_seconds or 0 for m in meetings)
    total_words = sum(m.word_count or 0 for m in meetings)
    merged_transcript = json.dumps(
        {"segments": merged_segments, "language": earliest.language or "en"}
    )

    # Create new merged meeting.
    new_id = await _repo.create_meeting(
        started_at=earliest.started_at,
        status="complete",
    )
    await _repo.update_meeting(
        new_id,
        title=f"Merged: {earliest.title}",
        ended_at=latest.ended_at,
        duration_seconds=total_duration,
        transcript_json=merged_transcript,
        tags=earliest.tags,
        language=earliest.language,
        word_count=total_words,
        label=earliest.label,
    )

    # Delete original meetings.
    for m in meetings:
        await _repo.delete_meeting(m.id)

    return {"meeting_id": new_id, "title": f"Merged: {earliest.title}"}


@router.get("/api/meetings/tags", summary="Get distinct meeting tags")
async def get_meeting_tags():
    return {"tags": await _repo.get_distinct_tags()}


@router.get(
    "/api/meetings/stats",
    response_model=MeetingStatsResponse,
    summary="Aggregate meeting stats",
)
async def get_meeting_stats():
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")
    return await _repo.get_stats()


@router.get("/api/meetings/{meeting_id}", summary="Get meeting by ID")
async def get_meeting(meeting_id: str):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting.to_dict()


@router.delete(
    "/api/meetings/{meeting_id}", response_model=DeleteResponse, summary="Delete meeting"
)
async def delete_meeting(meeting_id: str):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Delete audio file if it exists and is within allowed directories.
    if meeting.audio_path and os.path.exists(meeting.audio_path):
        resolved = Path(meeting.audio_path).resolve()
        allowed_dirs = [
            default_audio_dir().resolve(),
        ]
        try:
            allowed_dirs.append(Path(load_config().audio.temp_audio_dir).expanduser().resolve())
        except Exception:
            allowed_dirs.append(Path("/tmp/contextrecall").resolve())
        if any(resolved.is_relative_to(d) for d in allowed_dirs):
            try:
                os.remove(meeting.audio_path)
            except OSError:
                pass
        else:
            logger.warning("Skipping audio delete — path outside allowed directories: %s", resolved)

    await _repo.delete_meeting(meeting_id)
    return {"deleted": True}


@router.get("/api/meetings/{meeting_id}/audio", summary="Download meeting audio")
async def get_meeting_audio(meeting_id: str):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if not meeting.audio_path or not os.path.exists(meeting.audio_path):
        raise HTTPException(status_code=404, detail="Audio file not found")

    # Validate the audio file is within an expected directory.
    resolved = Path(meeting.audio_path).resolve()
    allowed_dirs = [
        default_audio_dir().resolve(),
    ]
    try:
        allowed_dirs.append(Path(load_config().audio.temp_audio_dir).expanduser().resolve())
    except Exception:
        allowed_dirs.append(Path("/tmp/contextrecall").resolve())
    if not any(resolved.is_relative_to(d) for d in allowed_dirs):
        raise HTTPException(status_code=403, detail="Audio file not found")

    return FileResponse(
        str(resolved),
        media_type="audio/wav",
        filename=f"meeting_{meeting_id}.wav",
    )


@router.patch("/api/meetings/{meeting_id}/label", summary="Set meeting label")
async def set_meeting_label(meeting_id: str, body: SetLabelRequest):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await _repo.update_meeting(meeting_id, label=body.label)
    return {"meeting_id": meeting_id, "label": body.label}


@router.patch("/api/meetings/{meeting_id}/tags", summary="Set meeting tags")
async def set_meeting_tags(meeting_id: str, body: SetTagsRequest):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    normalised: list[str] = []
    for raw in body.tags:
        tag = raw.strip()
        if tag and tag not in normalised:
            normalised.append(tag)
    await _repo.update_meeting(meeting_id, tags=normalised)
    return {"meeting_id": meeting_id, "tags": normalised}


@router.put(
    "/api/meetings/{meeting_id}/calendar-link", summary="Link a recording to a calendar event"
)
async def link_meeting_calendar(meeting_id: str, body: CalendarLinkRequest):
    from src.calendar_events.reader import CalendarEvent
    from src.calendar_link import CalendarLinkConflict, link_meeting_to_event

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    event = CalendarEvent(
        event_uid=body.event_uid,
        title=body.title,
        start_ts=body.start_ts,
        end_ts=body.end_ts,
        attendees=[a.model_dump() for a in body.attendees],
        organizer=body.organizer.model_dump() if body.organizer else None,
        join_url=body.join_url,
        meeting_id=body.meeting_id,
        calendar_name=body.calendar_name,
    )
    try:
        await link_meeting_to_event(_repo, _calendar_event_repo, meeting, event, source="manual")
    except CalendarLinkConflict as e:
        raise HTTPException(status_code=409, detail=str(e))

    if _event_bus is not None:
        _event_bus.emit(
            {
                "type": "meeting.calendar_link",
                "meeting_id": meeting_id,
                "calendar_event_uid": body.event_uid,
            }
        )
    updated = await _repo.get_meeting(meeting_id)
    return updated.to_dict()


@router.delete(
    "/api/meetings/{meeting_id}/calendar-link", summary="Unlink a recording from its calendar event"
)
async def unlink_meeting_calendar(meeting_id: str):
    from src.calendar_link import unlink_meeting_from_event

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await unlink_meeting_from_event(_repo, _calendar_event_repo, meeting)
    if _event_bus is not None:
        _event_bus.emit(
            {"type": "meeting.calendar_link", "meeting_id": meeting_id, "calendar_event_uid": ""}
        )
    return {"meeting_id": meeting_id, "calendar_event_uid": ""}


@router.patch("/api/meetings/{meeting_id}", summary="Rename a meeting")
async def rename_meeting(meeting_id: str, body: RenameMeetingRequest):
    # Pydantic's min_length=1 lets "   " through — reject a title that is
    # empty after stripping, before touching the row (I5).
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title must not be empty")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    import asyncio

    from src.meeting_rename import apply_rename

    return await apply_rename(
        _repo,
        meeting,
        title,
        config=load_config(),
        event_bus=_event_bus,
        loop=asyncio.get_running_loop(),
    )
