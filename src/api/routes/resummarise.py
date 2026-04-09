"""
Re-summarise endpoint.

POST /api/meetings/{id}/resummarise — re-run summarisation on an existing transcript.
"""

import asyncio
import json
import logging

import yaml
from fastapi import APIRouter, HTTPException

from src.summariser import Summariser
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import SummarisationConfig, _build_dataclass, DEFAULT_CONFIG_PATH

logger = logging.getLogger("meetingmind.api.resummarise")

router = APIRouter()

_repo = None


def init(repo) -> None:
    global _repo
    _repo = repo


def _load_summarisation_config() -> SummarisationConfig:
    """Read the current summarisation config from config.yaml."""
    try:
        with open(DEFAULT_CONFIG_PATH) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}
    return _build_dataclass(SummarisationConfig, raw.get("summarisation", {}))


def _reconstruct_transcript(transcript_json: str, duration: float) -> Transcript:
    """Rebuild a Transcript object from the stored JSON."""
    data = json.loads(transcript_json)
    segments = [
        TranscriptSegment(
            start=s.get("start", 0),
            end=s.get("end", 0),
            text=s.get("text", ""),
            speaker=s.get("speaker", ""),
        )
        for s in data.get("segments", [])
    ]
    return Transcript(
        segments=segments,
        language=data.get("language", ""),
        language_probability=data.get("language_probability", 0.0),
        duration_seconds=data.get("duration_seconds", duration or 0),
    )


@router.post("/api/meetings/{meeting_id}/resummarise")
async def resummarise_meeting(meeting_id: str):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    meeting = await _repo.get_meeting(meeting_id)

    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not meeting.transcript_json:
        raise HTTPException(status_code=400, detail="No transcript available for this meeting")

    config = _load_summarisation_config()
    transcript = _reconstruct_transcript(
        meeting.transcript_json, meeting.duration_seconds or 0
    )

    logger.info("Re-summarising meeting %s (%d segments)", meeting_id, len(transcript.segments))

    try:
        summariser = Summariser(config)
        summary = await asyncio.to_thread(summariser.summarise, transcript)
    except Exception as e:
        logger.error("Re-summarisation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Summarisation failed. Check server logs for details.")

    await _repo.update_meeting(
        meeting_id,
        title=summary.title,
        summary_markdown=summary.raw_markdown,
        tags=summary.tags,
    )
    await _repo.update_fts(meeting_id)

    logger.info("Re-summarisation complete: '%s'", summary.title)
    return {
        "meeting_id": meeting_id,
        "title": summary.title,
        "tags": summary.tags,
    }
