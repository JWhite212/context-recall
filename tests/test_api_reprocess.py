"""Tests for src/api/routes/reprocess.py — async pipeline submission.

Bug C4: the previous version awaited the full transcribe + summarise
pipeline inside the HTTP request handler. For long meetings the request
sat blocked for minutes, hit browser/uvicorn timeouts, and the user saw
an opaque HTTP error even though the daemon kept running and eventually
updated the DB. The endpoint now submits the pipeline as a background
asyncio task and returns 202 Accepted immediately, so the UI gets
instant feedback and the existing pipeline.* events drive the result UI.
"""

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


class _InFlightTracker:
    """Stand-in for the DB-backed reprocess_jobs table used by tests.

    The endpoint persists in-flight state via repo.add_reprocess_job /
    is_reprocess_in_flight / complete_reprocess_job. Tests need a small
    helper to mirror that shape without spinning up a real DB.
    """

    def __init__(self) -> None:
        self._set: set[str] = set()

    def install(self, repo) -> None:
        async def _add(mid: str) -> None:
            self._set.add(mid)

        async def _complete(mid: str) -> None:
            self._set.discard(mid)

        async def _is_in_flight(mid: str) -> bool:
            return mid in self._set

        repo.add_reprocess_job = AsyncMock(side_effect=_add)
        repo.complete_reprocess_job = AsyncMock(side_effect=_complete)
        repo.is_reprocess_in_flight = AsyncMock(side_effect=_is_in_flight)

    def __contains__(self, item: str) -> bool:
        return item in self._set

    def add(self, mid: str) -> None:
        self._set.add(mid)


def _make_meeting(meeting_id="m1", audio_path="/tmp/x.wav"):
    m = MagicMock()
    m.id = meeting_id
    m.audio_path = audio_path
    m.started_at = 1000.0
    return m


def _make_repo(meeting=None):
    """Create a MagicMock repo with the reprocess_jobs methods wired up."""
    repo = MagicMock()
    if meeting is not None:
        repo.get_meeting = AsyncMock(return_value=meeting)
    else:
        repo.get_meeting = AsyncMock(return_value=None)
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()
    tracker = _InFlightTracker()
    tracker.install(repo)
    repo._in_flight = tracker  # convenience handle for tests
    return repo


def _make_transcript():
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="hello world test")],
        language="en",
        language_probability=0.99,
        duration_seconds=2.0,
    )


def _make_short_transcript():
    """A real-but-short transcript (under the < 5 word threshold)."""
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="hi bye")],
        language="en",
        language_probability=0.99,
        duration_seconds=2.0,
    )


def _make_empty_transcript():
    """An empty transcript — what comes back when capture produced silence."""
    return Transcript(segments=[], language="en", language_probability=0.0, duration_seconds=0.0)


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

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

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

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))
    repo._in_flight.add("m1")

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 409


def test_reprocess_404_when_meeting_missing():
    repo = _make_repo(meeting=None)

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/missing/reprocess", headers=_auth_headers())
        assert resp.status_code == 404


def test_reprocess_400_when_no_audio_file(tmp_path):
    """Audio path on the row but the file no longer exists on disk."""
    repo = _make_repo(meeting=_make_meeting(audio_path="/no/such/file.wav"))

    app = _make_app(repo)
    with TestClient(app) as c:
        resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
        assert resp.status_code == 400


def test_background_task_eventually_marks_meeting_complete(tmp_path):
    """After the 202 returns, the background task must finish the work
    and write status='complete' + the transcript/summary fields."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

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
                # returned, but we also need the in-flight marker to drain.
                deadline = time.monotonic() + 2.0
                while "m1" in repo._in_flight and time.monotonic() < deadline:
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
    assert "m1" not in repo._in_flight, "in-flight marker must be cleared on completion"
    repo.complete_reprocess_job.assert_awaited_with("m1")


def test_background_task_marks_meeting_error_on_pipeline_failure(tmp_path):
    """A pipeline exception in the background task must be caught and
    written to the DB as status='error'. The HTTP request already
    returned 202, so it cannot raise."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

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
                while "m1" in repo._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert "error" in statuses, f"pipeline failure must mark the meeting 'error'; got {statuses}"
    assert "m1" not in repo._in_flight


# ---------------------------------------------------------------------------
# Bug B1 unification: reprocess must mirror the orchestrator's contract for
# short-but-non-empty transcripts — preserve them and mark 'complete' rather
# than raising. The orchestrator was fixed in commit 4847c5d; reprocess was
# deliberately left out of that commit to limit blast radius. This is the
# follow-up that closes the asymmetry: clicking "Retry Transcription" on a
# 2-word meeting now produces the same outcome as the auto-detect path.
# ---------------------------------------------------------------------------


def test_short_transcript_marks_meeting_complete_not_error(tmp_path):
    """A short-but-non-empty transcript (e.g. "hi bye") must be preserved
    and the meeting marked 'complete'. Previously _run_pipeline raised
    ValueError on word_count < 5, which the caller mapped to status='error'
    — losing the real transcript the user actually had captured."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

    mock_transcriber = MagicMock()
    mock_transcriber.transcribe.return_value = _make_short_transcript()

    app = _make_app(repo)
    with TestClient(app) as c:
        with patch("src.api.routes.reprocess.Transcriber", return_value=mock_transcriber):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                deadline = time.monotonic() + 2.0
                while "m1" in repo._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]

    # The meeting MUST NOT be flagged 'error' just for being short — that
    # conflates "no audio at all" with "very short conversation".
    assert "error" not in statuses, (
        "short-but-non-empty transcripts must not be flagged 'error' on "
        f"reprocess; got status sequence {statuses}"
    )
    assert "complete" in statuses, (
        "short transcript must be marked 'complete' so the user can see "
        f"what was captured; got status sequence {statuses}"
    )

    # The transcript itself must be persisted so the user can review it.
    transcript_calls = [
        call for call in repo.update_meeting.await_args_list if "transcript_json" in call.kwargs
    ]
    assert transcript_calls, (
        "transcript_json must be persisted for short transcripts so the "
        "user can review the captured content"
    )


def test_short_transcript_emits_pipeline_complete_not_error(tmp_path):
    """The UI's pipelineStage clears on pipeline.complete and rolls back to
    an alert on pipeline.error. Short transcripts must take the .complete
    path so the user doesn't see a misleading red banner for what was a
    successful (if brief) reprocess."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

    mock_transcriber = MagicMock()
    mock_transcriber.transcribe.return_value = _make_short_transcript()

    event_bus = MagicMock()

    app = _make_app(repo, event_bus=event_bus)
    with TestClient(app) as c:
        with patch("src.api.routes.reprocess.Transcriber", return_value=mock_transcriber):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                deadline = time.monotonic() + 2.0
                while "m1" in repo._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    emitted_types = [
        call.args[0].get("type")
        for call in event_bus.emit.call_args_list
        if call.args and isinstance(call.args[0], dict)
    ]
    assert "pipeline.complete" in emitted_types, (
        f"short transcript must emit pipeline.complete; got {emitted_types}"
    )
    assert "pipeline.error" not in emitted_types, (
        f"short transcript must not emit pipeline.error; got {emitted_types}"
    )


def test_empty_transcript_still_marks_meeting_error(tmp_path):
    """Counterpart: a truly empty transcript (no segments) is a real
    failure — capture produced silence or corrupted audio. The meeting
    must still be flagged 'error' in that case, mirroring the
    orchestrator's empty-transcript branch."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

    mock_transcriber = MagicMock()
    mock_transcriber.transcribe.return_value = _make_empty_transcript()

    app = _make_app(repo)
    with TestClient(app) as c:
        with patch("src.api.routes.reprocess.Transcriber", return_value=mock_transcriber):
            with patch(
                "src.api.routes.reprocess._load_config_sections",
                return_value=(MagicMock(), MagicMock()),
            ):
                resp = c.post("/api/meetings/m1/reprocess", headers=_auth_headers())
                assert resp.status_code == 202

                deadline = time.monotonic() + 2.0
                while "m1" in repo._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    statuses = [
        call.kwargs.get("status")
        for call in repo.update_meeting.await_args_list
        if "status" in call.kwargs
    ]
    assert "error" in statuses, f"empty transcript must mark meeting 'error'; got {statuses}"


def test_background_task_emits_pipeline_complete_event(tmp_path):
    """When the background pipeline succeeds, an event must be emitted
    so the UI's existing pipeline.complete listener clears pipelineStage
    and refetches the meeting (mirrors the orchestrator's behavior)."""
    audio_file = tmp_path / "x.wav"
    audio_file.write_bytes(b"\x00" * 100)

    repo = _make_repo(meeting=_make_meeting(audio_path=str(audio_file)))

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
                while "m1" in repo._in_flight and time.monotonic() < deadline:
                    time.sleep(0.05)

    emitted_types = [
        call.args[0].get("type")
        for call in event_bus.emit.call_args_list
        if call.args and isinstance(call.args[0], dict)
    ]
    assert "pipeline.complete" in emitted_types, (
        f"expected pipeline.complete event; got {emitted_types}"
    )
