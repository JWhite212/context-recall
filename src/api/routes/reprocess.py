"""
Reprocess endpoint.

POST /api/meetings/{id}/reprocess — re-run the full pipeline (transcribe →
summarise) on a meeting's existing audio file. Useful for retrying after
transient errors (e.g. missing ffmpeg, OOM, timeout).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from src.summariser import Summariser
from src.templates import TemplateManager
from src.transcriber import Transcriber
from src.utils.config import (
    DEFAULT_CONFIG_PATH,
    SummarisationConfig,
    TranscriptionConfig,
    _build_dataclass,
)

logger = logging.getLogger("meetingmind.api.reprocess")

router = APIRouter()

_repo = None
_in_flight: set[str] = set()


def init(repo) -> None:
    global _repo
    _repo = repo


def _load_config_sections() -> tuple[TranscriptionConfig, SummarisationConfig]:
    """Read transcription and summarisation config from config.yaml."""
    try:
        with open(DEFAULT_CONFIG_PATH) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raw = {}
    return (
        _build_dataclass(TranscriptionConfig, raw.get("transcription", {})),
        _build_dataclass(SummarisationConfig, raw.get("summarisation", {})),
    )


def _run_pipeline(
    audio_path: Path,
    trans_config: TranscriptionConfig,
    summ_config: SummarisationConfig,
) -> dict:
    """Run transcribe → summarise on a background thread. Returns result dict."""
    transcriber = Transcriber(trans_config)
    transcript = transcriber.transcribe(audio_path)

    if transcript.word_count < 5:
        raise ValueError(
            f"Transcript too short ({transcript.word_count} words). "
            f"The audio may be silent or corrupted."
        )

    # Load default template.
    template = None
    try:
        tm = TemplateManager()
        template = tm.get_template(summ_config.default_template)
    except Exception:
        pass

    summariser = Summariser(summ_config)
    summary = summariser.summarise(transcript, template=template)

    return {
        "transcript": transcript,
        "summary": summary,
    }


@router.post(
    "/api/meetings/{meeting_id}/reprocess",
    summary="Reprocess meeting from audio",
)
async def reprocess_meeting(meeting_id: str):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    if meeting_id in _in_flight:
        raise HTTPException(status_code=409, detail="Reprocessing already in progress")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not meeting.audio_path or not os.path.exists(meeting.audio_path):
        raise HTTPException(
            status_code=400,
            detail="No audio file available for this meeting",
        )

    audio_path = Path(meeting.audio_path)
    trans_config, summ_config = _load_config_sections()

    logger.info("Reprocessing meeting %s from %s", meeting_id, audio_path)

    # Mark as transcribing.
    await _repo.update_meeting(meeting_id, status="transcribing")

    _in_flight.add(meeting_id)
    try:
        result = await asyncio.to_thread(
            _run_pipeline,
            audio_path,
            trans_config,
            summ_config,
        )
    except Exception as e:
        logger.error("Reprocessing failed: %s", e, exc_info=True)
        await _repo.update_meeting(meeting_id, status="error")
        raise HTTPException(
            status_code=500,
            detail="Reprocessing failed. Check daemon logs for details.",
        )
    finally:
        _in_flight.discard(meeting_id)

    transcript = result["transcript"]
    summary = result["summary"]

    await _repo.update_meeting(
        meeting_id,
        title=summary.title,
        ended_at=meeting.started_at + transcript.duration_seconds,
        duration_seconds=transcript.duration_seconds,
        status="complete",
        transcript_json=json.dumps(transcript.to_dict()),
        summary_markdown=summary.raw_markdown,
        tags=summary.tags,
        language=transcript.language,
        word_count=transcript.word_count,
    )
    await _repo.update_fts(meeting_id)

    logger.info("Reprocessing complete: '%s'", summary.title)
    return {
        "meeting_id": meeting_id,
        "title": summary.title,
        "status": "complete",
    }
