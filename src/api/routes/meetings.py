"""
Meeting history CRUD endpoints.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.utils.config import load_config

router = APIRouter()

# Injected at startup.
_repo = None


def init(repo):
    global _repo
    _repo = repo


@router.get("/api/meetings")
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


@router.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting.to_dict()


@router.delete("/api/meetings/{meeting_id}")
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


@router.get("/api/meetings/{meeting_id}/audio")
async def get_meeting_audio(meeting_id: str):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if not meeting.audio_path or not os.path.exists(meeting.audio_path):
        raise HTTPException(status_code=404, detail="Audio file not found")

    # Validate the audio file is within the expected directory.
    try:
        audio_dir = Path(load_config().audio.temp_audio_dir).expanduser().resolve()
    except Exception:
        audio_dir = Path("/tmp/meetingmind").resolve()
    resolved = Path(meeting.audio_path).resolve()
    if not str(resolved).startswith(str(audio_dir)):
        raise HTTPException(status_code=403, detail="Audio file not found")

    return FileResponse(
        str(resolved),
        media_type="audio/wav",
        filename=f"meeting_{meeting_id}.wav",
    )
