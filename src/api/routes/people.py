"""
People directory endpoints.

GET    /api/people                                  — list people (+ sample counts)
POST   /api/people                                  — create a person
PATCH  /api/people/{id}                             — update a person
DELETE /api/people/{id}                             — delete (cascades voice samples)
GET    /api/people/{id}/voice-samples               — enrolment sample metadata
DELETE /api/people/{id}/voice-samples/{sample_id}   — remove one sample
POST   /api/meetings/{mid}/speakers/{sid}/assign-person
       — label a transcript speaker as a person and (optionally) enrol
         their voice from this meeting's audio for future auto-naming.
"""

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.utils.config import load_config

logger = logging.getLogger("contextrecall.api.people")

router = APIRouter()

_repo = None  # MeetingRepository
_person_repo = None  # PersonRepository

_SPEAKER_ID_RE = re.compile(r"^[a-zA-Z0-9_ -]+$")


def init(repo, person_repo) -> None:
    global _repo, _person_repo
    _repo = repo
    _person_repo = person_repo


def _require_repos() -> None:
    if not _repo or not _person_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class PersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=320)
    aliases: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)
    is_me: bool = False


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    aliases: list[str] | None = None
    notes: str | None = Field(default=None, max_length=4000)
    is_me: bool | None = None


class AssignPersonRequest(BaseModel):
    person_id: str = Field(min_length=1)
    enrol_voice: bool = True


@router.get("/api/people")
async def list_people():
    _require_repos()
    return await _person_repo.list_people()


@router.post("/api/people", status_code=201)
async def create_person(body: PersonCreate):
    _require_repos()
    person_id = await _person_repo.create(
        name=body.name.strip(),
        email=body.email.strip(),
        aliases=[a.strip() for a in body.aliases if a.strip()],
        notes=body.notes,
        is_me=body.is_me,
    )
    return await _person_repo.get(person_id)


@router.patch("/api/people/{person_id}")
async def update_person(person_id: str, body: PersonUpdate):
    _require_repos()
    if not await _person_repo.get(person_id):
        raise HTTPException(status_code=404, detail="Person not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.email is not None:
        fields["email"] = body.email.strip()
    if body.aliases is not None:
        fields["aliases_json"] = [a.strip() for a in body.aliases if a.strip()]
    if body.notes is not None:
        fields["notes"] = body.notes
    if body.is_me is not None:
        fields["is_me"] = body.is_me
    if fields:
        await _person_repo.update(person_id, **fields)
    return await _person_repo.get(person_id)


@router.delete("/api/people/{person_id}")
async def delete_person(person_id: str):
    _require_repos()
    deleted = await _person_repo.delete(person_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Person not found")
    return {"deleted": person_id}


@router.get("/api/people/{person_id}/voice-samples")
async def list_voice_samples(person_id: str):
    _require_repos()
    if not await _person_repo.get(person_id):
        raise HTTPException(status_code=404, detail="Person not found")
    return await _person_repo.list_voice_samples(person_id)


@router.delete("/api/people/{person_id}/voice-samples/{sample_id}")
async def delete_voice_sample(person_id: str, sample_id: int):
    _require_repos()
    deleted = await _person_repo.delete_voice_sample(sample_id, person_id=person_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Voice sample not found")
    return {"deleted": sample_id}


@router.post("/api/meetings/{meeting_id}/speakers/{speaker_id}/assign-person")
async def assign_person(meeting_id: str, speaker_id: str, body: AssignPersonRequest):
    """Label a transcript speaker as a known person.

    Renames the speaker throughout the meeting (same as the manual
    rename endpoint, but linked to the person), then — when the audio
    still exists — extracts that speaker's segments as a voice-profile
    enrolment sample so future meetings recognise them automatically.
    """
    _require_repos()
    if not _SPEAKER_ID_RE.match(speaker_id):
        raise HTTPException(status_code=422, detail="Invalid speaker_id format")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    person = await _person_repo.get(body.person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    voice_cfg = load_config().voice_id

    # Capture the speaker's windows BEFORE the rename rewrites the
    # transcript labels.
    from src.voice.enrolment import extract_speaker_windows

    windows = extract_speaker_windows(
        meeting.transcript_json, speaker_id, voice_cfg.min_segment_seconds
    )

    await _repo.set_speaker_name(
        meeting_id,
        speaker_id,
        person["name"],
        source="manual",
        person_id=body.person_id,
    )

    enrolled = False
    reason = None
    if body.enrol_voice:
        enrolled, reason = await _try_enrol(meeting, person, speaker_id, windows, voice_cfg)

    refreshed = await _person_repo.get(body.person_id)
    return {
        "meeting_id": meeting_id,
        "speaker_id": speaker_id,
        "person_id": body.person_id,
        "display_name": person["name"],
        "enrolled": enrolled,
        "reason": reason,
        "sample_count": refreshed["sample_count"] if refreshed else 0,
    }


async def _try_enrol(meeting, person, speaker_id, windows, voice_cfg) -> tuple[bool, str | None]:
    """Best-effort voice enrolment; failures explain themselves."""
    from src.voice.embedder import VoiceEmbedder, is_voice_id_available
    from src.voice.enrolment import build_enrolment_sample

    if not is_voice_id_available():
        return False, "voice identification not available (speechbrain not installed)"
    if not windows:
        return False, "no segments long enough to build a voice profile"
    if not meeting.audio_path or not Path(meeting.audio_path).exists():
        return False, "meeting audio no longer available"

    try:
        embedder = VoiceEmbedder(voice_cfg.model_source)
        sample = await asyncio.to_thread(
            build_enrolment_sample, embedder, Path(meeting.audio_path), windows
        )
    except Exception as e:
        logger.warning("Voice enrolment failed for %s: %s", person["id"], e)
        return False, f"enrolment failed: {e}"
    if sample is None:
        return False, "audio windows were silent or unusable"

    await _person_repo.add_voice_sample(
        person["id"],
        sample["embedding"],
        source_meeting_id=meeting.id,
        speaker_label=speaker_id,
        segment_count=sample["segment_count"],
        duration_seconds=sample["duration_seconds"],
        max_samples=voice_cfg.max_samples_per_person,
    )
    logger.info(
        "Enrolled voice sample for %s from meeting %s (%d segments, %.1fs)",
        person["name"],
        meeting.id,
        sample["segment_count"],
        sample["duration_seconds"],
    )
    return True, None
