"""
Reprocess endpoint.

POST /api/meetings/{id}/reprocess — re-run the full pipeline (transcribe →
summarise) on a meeting's existing audio file. Useful for retrying after
transient errors (e.g. missing ffmpeg, OOM, timeout) or recovering meetings
left in 'transcribing' by a daemon crash.

The endpoint submits the pipeline as a background asyncio task and returns
202 Accepted immediately so long meetings can't time out the HTTP request
(Bug C4). The UI relies on the existing pipeline.* WebSocket events plus
react-query invalidation on `pipeline.complete` to surface the result.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.summariser import Summariser
from src.templates import TemplateManager
from src.transcriber import Transcriber
from src.utils.config import (
    DEFAULT_CONFIG_PATH,
    SummarisationConfig,
    TranscriptionConfig,
    _build_dataclass,
)

logger = logging.getLogger("contextrecall.api.reprocess")

router = APIRouter()

_repo = None
_event_bus = None
_in_flight: set[str] = set()


def init(repo, event_bus=None) -> None:
    global _repo, _event_bus
    _repo = repo
    _event_bus = event_bus


def _emit(event: dict) -> None:
    """Push an event to subscribers if the event bus is wired up.

    Mirrors the orchestrator's pipeline.* event shapes so the UI's
    existing handlers (appStore, usePipelineSync) can drive the result
    UI without knowing the work came from a reprocess vs auto-detect.
    """
    if _event_bus is None:
        return
    try:
        _event_bus.emit(event)
    except Exception:
        logger.warning("Failed to emit reprocess event", exc_info=True)


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


async def _do_reprocess(
    meeting_id: str,
    audio_path: Path,
    started_at: float,
    trans_config: TranscriptionConfig,
    summ_config: SummarisationConfig,
) -> None:
    """Background task: run the pipeline, update the DB, emit events.

    Runs after the HTTP request has already returned 202. Any failure is
    captured and reflected on the meeting row as status='error' — there
    is nowhere to raise to since the client connection is gone.
    """
    try:
        _emit({"type": "pipeline.stage", "meeting_id": meeting_id, "stage": "transcribing"})
        try:
            result = await asyncio.to_thread(_run_pipeline, audio_path, trans_config, summ_config)
        except Exception as e:
            logger.error("Reprocessing failed for %s: %s", meeting_id, e, exc_info=True)
            try:
                await _repo.update_meeting(meeting_id, status="error")
            except Exception:
                logger.error(
                    "Failed to mark meeting %s as error after pipeline failure",
                    meeting_id,
                    exc_info=True,
                )
            _emit(
                {
                    "type": "pipeline.error",
                    "meeting_id": meeting_id,
                    "stage": "transcribing",
                    "error": str(e),
                }
            )
            return

        transcript = result["transcript"]
        summary = result["summary"]

        try:
            await _repo.update_meeting(
                meeting_id,
                title=summary.title,
                ended_at=started_at + transcript.duration_seconds,
                duration_seconds=transcript.duration_seconds,
                status="complete",
                transcript_json=json.dumps(transcript.to_dict()),
                summary_markdown=summary.raw_markdown,
                tags=summary.tags,
                language=transcript.language,
                word_count=transcript.word_count,
            )
            await _repo.update_fts(meeting_id)
        except Exception:
            logger.error(
                "Failed to persist reprocess result for %s",
                meeting_id,
                exc_info=True,
            )
            _emit(
                {
                    "type": "pipeline.error",
                    "meeting_id": meeting_id,
                    "stage": "writing",
                    "error": "Failed to persist reprocess result.",
                }
            )
            return

        logger.info("Reprocessing complete: '%s'", summary.title)
        _emit({"type": "pipeline.complete", "meeting_id": meeting_id, "title": summary.title})
    finally:
        _in_flight.discard(meeting_id)


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

    logger.info("Reprocessing meeting %s from %s (background)", meeting_id, audio_path)

    # Mark as transcribing synchronously so an immediately-following GET
    # of the meeting returns the in-flight status.
    await _repo.update_meeting(meeting_id, status="transcribing")

    _in_flight.add(meeting_id)
    asyncio.create_task(
        _do_reprocess(meeting_id, audio_path, meeting.started_at, trans_config, summ_config)
    )

    return JSONResponse(
        status_code=202,
        content={"meeting_id": meeting_id, "status": "accepted"},
    )
