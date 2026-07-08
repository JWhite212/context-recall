"""
Reprocess endpoint.

POST /api/meetings/{id}/reprocess — re-run the FULL pipeline on a
meeting's existing audio file via the shared ``PipelineRunner``:
transcribe → diarise (when the capture's source WAVs still survive in
the temp dir) → speaker enrichment (stored attendees + the user's saved
renames) → summarise → persist/FTS → embeddings → markdown/Notion
outputs → action items and analytics. The route used to re-implement a
subset of the orchestrator's pipeline and drifted (no diarisation, no
writers, no embeddings); it now shares the exact stage sequence.

The endpoint submits the pipeline as a background asyncio task and
returns 202 Accepted immediately so long meetings can't time out the
HTTP request (Bug C4). The UI relies on the existing pipeline.*
WebSocket events plus react-query invalidation on `pipeline.complete`
to surface the result.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.pipeline_runner import DbBridge, PipelineRunner, derive_source_paths
from src.utils.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger("contextrecall.api.reprocess")

router = APIRouter()

_repo = None
_event_bus = None
_db = None


def init(repo, event_bus=None, db=None) -> None:
    global _repo, _event_bus, _db
    _repo = repo
    _event_bus = event_bus
    _db = db


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


def _make_runner(config, emit, bridge) -> PipelineRunner:
    """Build the pipeline runner from fresh config (module-level seam
    so tests can substitute a fake runner)."""
    return PipelineRunner.from_config(config, emit=emit, db=bridge)


def _stored_attendees(meeting) -> list[dict]:
    """Parse the attendees captured when the meeting was first recorded."""
    try:
        attendees = json.loads(meeting.attendees_json or "[]")
    except (ValueError, TypeError):
        return []
    return attendees if isinstance(attendees, list) else []


async def _do_reprocess(meeting, config) -> None:
    """Background task: run the shared pipeline, then clear the job row.

    Runs after the HTTP request has already returned 202. The runner
    handles per-stage failures itself (status='error' + pipeline.error
    events); the except here is belt-and-braces for unexpected crashes
    so the row can never stick in 'transcribing'.
    """
    loop = asyncio.get_running_loop()
    bridge = DbBridge(_repo, loop, database=_db)

    def emit(event_type: str, **kwargs) -> None:
        _emit({"type": event_type, **kwargs})

    audio_path = Path(meeting.audio_path)
    # The energy diariser needs the separate mic source WAV. It lives in
    # the temp dir until the retention sweep removes it; when it is gone
    # the runner degrades to an undiarised transcript with a visible
    # pipeline.warning instead of failing.
    sources = derive_source_paths(audio_path, config.audio.temp_audio_dir)

    runner = _make_runner(config, emit, bridge)
    try:
        result = await asyncio.to_thread(
            runner.run,
            audio_path,
            meeting.id,
            meeting.started_at,
            0.0,
            attendees=_stored_attendees(meeting),
            mic_audio_path=sources["mic"],
            preserve_mappings=True,
            notion_page_id=(getattr(meeting, "notion_page_id", "") or None),
            is_reprocess=True,
        )
        logger.info("Reprocessing finished for %s: %s", meeting.id, result.status)
    except Exception as e:
        logger.error("Reprocessing failed for %s: %s", meeting.id, e, exc_info=True)
        try:
            await _repo.update_meeting(meeting.id, status="error")
        except Exception:
            logger.error(
                "Failed to mark meeting %s as error after pipeline failure",
                meeting.id,
                exc_info=True,
            )
        _emit(
            {
                "type": "pipeline.error",
                "meeting_id": meeting.id,
                "stage": "transcribing",
                "error": str(e),
            }
        )
    finally:
        try:
            await _repo.complete_reprocess_job(meeting.id)
        except Exception:
            logger.warning(
                "Failed to clear reprocess job row for %s",
                meeting.id,
                exc_info=True,
            )


@router.post(
    "/api/meetings/{meeting_id}/reprocess",
    summary="Reprocess meeting from audio",
)
async def reprocess_meeting(meeting_id: str):
    if not _repo:
        raise HTTPException(status_code=503, detail="Repository not available")

    if await _repo.is_reprocess_in_flight(meeting_id):
        raise HTTPException(status_code=409, detail="Reprocessing already in progress")

    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not meeting.audio_path or not os.path.exists(meeting.audio_path):
        raise HTTPException(
            status_code=400,
            detail="No audio file available for this meeting",
        )

    # Fresh config per request so settings changes (model, backends,
    # writers) apply to a reprocess without restarting the daemon.
    config = load_config(DEFAULT_CONFIG_PATH)

    logger.info("Reprocessing meeting %s from %s (background)", meeting_id, meeting.audio_path)

    # Mark as transcribing synchronously so an immediately-following GET
    # of the meeting returns the in-flight status.
    await _repo.update_meeting(meeting_id, status="transcribing")

    # Persist the in-flight marker BEFORE returning 202 so a restart
    # between now and pipeline completion can recover this row.
    await _repo.add_reprocess_job(meeting_id)
    asyncio.create_task(_do_reprocess(meeting, config))

    return JSONResponse(
        status_code=202,
        content={"meeting_id": meeting_id, "status": "accepted"},
    )
