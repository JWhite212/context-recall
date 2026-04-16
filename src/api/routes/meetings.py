"""
Meeting history CRUD endpoints.
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from src.api.schemas import DeleteResponse, MeetingListResponse
from src.utils.config import load_config

router = APIRouter()

# Injected at startup.
_repo = None


def init(repo):
    global _repo
    _repo = repo


@router.get("/api/meetings", response_model=MeetingListResponse, summary="List meetings")
async def list_meetings(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    q: str | None = Query(None),
):
    if q:
        meetings = await _repo.search_meetings(q, limit=limit)
    else:
        meetings = await _repo.list_meetings(limit=limit, offset=offset, status=status)

    total = await _repo.count_meetings(status=status)

    return {
        "meetings": [m.to_dict() for m in meetings],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --- Routes below MUST be registered before /api/meetings/{meeting_id} ---


@router.post("/api/meetings/merge", summary="Merge multiple meetings into one")
async def merge_meetings(request: Request):
    body = await request.json()
    meeting_ids = body.get("meeting_ids", [])

    if len(meeting_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 meeting IDs required")

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


@router.get("/api/meetings/labels", summary="Get distinct meeting labels")
async def get_meeting_labels():
    labels = await _repo.get_distinct_labels()
    return {"labels": labels}


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

    # Delete audio file if it exists.
    if meeting.audio_path and os.path.exists(meeting.audio_path):
        try:
            os.remove(meeting.audio_path)
        except OSError:
            pass

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
        Path(os.path.expanduser("~/Library/Application Support/MeetingMind/audio")).resolve(),
    ]
    try:
        allowed_dirs.append(Path(load_config().audio.temp_audio_dir).expanduser().resolve())
    except Exception:
        allowed_dirs.append(Path("/tmp/meetingmind").resolve())
    if not any(resolved.is_relative_to(d) for d in allowed_dirs):
        raise HTTPException(status_code=403, detail="Audio file not found")

    return FileResponse(
        str(resolved),
        media_type="audio/wav",
        filename=f"meeting_{meeting_id}.wav",
    )


@router.patch("/api/meetings/{meeting_id}/label", summary="Set meeting label")
async def set_meeting_label(meeting_id: str, request: Request):
    body = await request.json()
    label = body.get("label", "")
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    await _repo.update_meeting(meeting_id, label=label)
    return {"meeting_id": meeting_id, "label": label}
