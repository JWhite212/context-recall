"""
Speaker name mapping endpoints.

PATCH /api/meetings/{id}/speakers/{speaker_id}  — set speaker display name
GET   /api/meetings/{id}/speakers               — get speaker names for a meeting
GET   /api/speakers                             — get all global speaker mappings
"""

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("meetingmind.api.speakers")

router = APIRouter()

_repo = None

_SPEAKER_ID_RE = re.compile(r"^[a-zA-Z0-9_ -]+$")


def init(repo) -> None:
    global _repo
    _repo = repo


class SpeakerNameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


class SpeakerMapping(BaseModel):
    speaker_id: str
    display_name: str
    source: str
    created_at: float


@router.patch("/api/meetings/{meeting_id}/speakers/{speaker_id}")
async def set_speaker_name(meeting_id: str, speaker_id: str, body: SpeakerNameRequest):
    if not _SPEAKER_ID_RE.match(speaker_id):
        raise HTTPException(status_code=422, detail="Invalid speaker_id format")

    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    await _repo.set_speaker_name(meeting_id, speaker_id, body.display_name)
    return {"meeting_id": meeting_id, "speaker_id": speaker_id, "display_name": body.display_name}


@router.get("/api/meetings/{meeting_id}/speakers", response_model=list[SpeakerMapping])
async def get_meeting_speakers(meeting_id: str):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    return await _repo.get_speaker_names(meeting_id)


@router.get("/api/speakers", response_model=list[SpeakerMapping])
async def get_global_speakers():
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    return await _repo.get_global_speaker_names()
