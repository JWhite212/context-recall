"""Tests for src/api/routes/reprocess.py — async pipeline submission.

Bug C4: the previous version awaited the full transcribe + summarise
pipeline inside the HTTP request handler. For long meetings the request
sat blocked for minutes, hit browser/uvicorn timeouts, and the user saw
an opaque HTTP error even though the daemon kept running and eventually
updated the DB. The endpoint now submits the pipeline as a background
asyncio task and returns 202 Accepted immediately, so the UI gets
instant feedback and the existing pipeline.* events drive the result UI.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import reprocess as reprocess_routes
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment

TEST_TOKEN = "test-token-for-reprocess-tests"


def _make_app(repo, event_bus=None) -> FastAPI:
    reprocess_routes.init(repo, event_bus)
    app = FastAPI()
    auth_deps = [Depends(verify_token)]
    app.include_router(reprocess_routes.router, dependencies=auth_deps)
    return app


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


@pytest.fixture(autouse=True)
def _reset_in_flight():
    """Each test starts with a clean _in_flight set."""
    reprocess_routes._in_flight.clear()
    yield
    reprocess_routes._in_flight.clear()


def _make_meeting(meeting_id="m1", audio_path="/tmp/x.wav"):
    m = MagicMock()
    m.id = meeting_id
    m.audio_path = audio_path
    m.started_at = 1000.0
    return m


def _make_transcript():
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="hello world test")],
        language="en",
        language_probability=0.99,
        duration_seconds=2.0,
    )


def _make_summary():
    return MeetingSummary(
        raw_markdown="# Test\n\n## Summary\nA test meeting.",
        title="Test Meeting",
        tags=["test"],
    )


def test_reprocess_returns_202_immediately_even_for_slow_pipelines(tmp_path):
    """The endpoint must return 202 Accepted within milliseconds, even
    when the underlying transcription would take many seconds. This is
    the core C4 fix: no more HTTP timeouts on long meetings."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path=str(audio_file)))
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()

    # Make the pipeline "slow" so the test would hang for 5s if the old
    # blocking behaviour were still in place.
    def slow_pipeline(*args, **kwargs):
        time.sleep(5)
        return {"transcript": _make_transcript(), "summary": _make_summary()}

    app = _make_app(repo)
    with TestClient(app) as c:
        with patch("src.api.routes.reprocess._run_pipeline", side_effect=slow_pipeline):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                start = time.monotonic()
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                elapsed = time.monotonic() - start

        assert resp.status_code == 202, (
            f"expected 202 Accepted; got {resp.status_code}: {resp.text}"
        )
        assert elapsed < 1.0, (
            f"endpoint returned in {elapsed:.2f}s; the 5s slow pipeline must run "
            "in the background, not in the HTTP request"
        )
        body = resp.json()
        assert body["meeting_id"] == "m1"
        assert body["status"] == "accepted"


def test_reprocess_409_when_already_in_flight(tmp_path):
    """Concurrent reprocess of the same meeting still returns 409."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path=str(audio_file)))
    repo.update_meeting = AsyncMock()

    reprocess_routes._in_flight.add("m1")

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 409


def test_reprocess_404_when_meeting_missing():
    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=None)

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/missing/reprocess", headers=_auth_headers())
        assert resp.status_code == 404


def test_reprocess_400_when_no_audio_file(tmp_path):
    """Audio path on the row but the file no longer exists on disk."""
    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path="/no/such/file.wav"))

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 400


def test_background_task_eventually_marks_meeting_complete(tmp_path):
    """After the 202 returns, the background task must finish the work
    and write status='complete' + the transcript/summary fields."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path=str(audio_file)))
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()

    app = _make_app(repo)
    with TestClient(app) as c:
        with patch(
            "src.api.routes.reprocess._run_pipeline",
            return_value={"transcript": _make_transcript(), "summary": _make_summary()},
        ):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                # Drain pending tasks. The TestClient's event loop has already
                # run the background task to completion by the time the request
                # returned, but we also need _in_flight to drain.
                deadline = time.monotonic() + 2.0
                while "m1" in reprocess_routes._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    # The endpoint marks the row 'transcribing' synchronously, then the
    # background task must mark it 'complete' once the pipeline finishes.
    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert "complete" in statuses, (
        f"expected status='complete' from background task; got status sequence {statuses}"
    )
    assert "m1" not in reprocess_routes._in_flight, "_in_flight must be cleared on completion"


def test_background_task_marks_meeting_error_on_pipeline_failure(tmp_path):
    """A pipeline exception in the background task must be caught and
    written to the DB as status='error'. The HTTP request already
    returned 202, so it cannot raise."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path=str(audio_file)))
    repo.update_meeting = AsyncMock()

    app = _make_app(repo)
    with TestClient(app) as c:
        with patch(
            "src.api.routes.reprocess._run_pipeline",
            side_effect=RuntimeError("MLX exploded"),
        ):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                deadline = time.monotonic() + 2.0
                while "m1" in reprocess_routes._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert "error" in statuses, f"pipeline failure must mark the meeting 'error'; got {statuses}"
    assert "m1" not in reprocess_routes._in_flight


def test_background_task_emits_pipeline_complete_event(tmp_path):
    """When the background pipeline succeeds, an event must be emitted
    so the UI's existing pipeline.complete listener clears pipelineStage
    and refetches the meeting (mirrors the orchestrator's behavior)."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = MagicMock()
    repo.get_meeting = AsyncMock(return_value=_make_meeting(audio_path=str(audio_file)))
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()

    event_bus = MagicMock()

    app = _make_app(repo, event_bus=event_bus)
    with TestClient(app) as c:
        with patch(
            "src.api.routes.reprocess._run_pipeline",
            return_value={"transcript": _make_transcript(), "summary": _make_summary()},
        ):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                deadline = time.monotonic() + 2.0
                while "m1" in reprocess_routes._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    emitted_types = [
        call.args[0].get("type")
        for call in event_bus.emit.call_args_list
        if call.args and isinstance(call.args[0], dict)
    ]
    assert "pipeline.complete" in emitted_types, (
        f"expected pipeline.complete event; got {emitted_types}"
    )
