"""Tests for src/main.py - Context Recall orchestrator with heavy mocking."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment


@pytest.fixture(autouse=True)
def _closed_db_bridge_try_call():
    """Close (not run) any repo coroutine handed to DbBridge.try_call.

    Orchestrator tests mock the API server with a MagicMock loop, which
    swallows call_soon_threadsafe: a real try_call would (a) leak the repo
    coroutine it was handed — surfacing later as "coroutine ... was never
    awaited" RuntimeWarnings attributed to whatever test gc runs under —
    and (b) stall ~10s per call in future.result() before its TimeoutError.
    Closing the coroutine and returning None is exactly what the real
    method does when the bridge is unavailable: same semantics, instant,
    warning-free. try_call's real behaviour is covered by
    tests/test_pipeline_runner.py.
    """
    from src.pipeline_runner import DbBridge

    def _closed_try_call(self, coro, timeout=15.0, what="db call"):
        coro.close()
        return None

    with patch.object(DbBridge, "try_call", _closed_try_call):
        yield


@pytest.fixture
def tmp_config(tmp_path):
    """Create a minimal config.yaml for Context Recall init."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {
            "sample_rate": 16000,
            "temp_audio_dir": str(tmp_path / "audio"),
        },
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": False},
        "notion": {"enabled": False},
        "diarisation": {"enabled": False},
        "api": {"enabled": False},
        "logging": {
            "level": "WARNING",
            "log_file": str(log_dir / "test.log"),
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture
def audio_file(tmp_path):
    """Create a minimal fake WAV file."""
    path = tmp_path / "test_recording.wav"
    # Minimal valid WAV header (44 bytes).
    path.write_bytes(b"RIFF" + b"\x00" * 40)
    return path


def _make_transcript(word_count_target=20):
    """Build a Transcript with enough words to pass the threshold."""
    words = " ".join(f"word{i}" for i in range(word_count_target))
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=60.0, text=words)],
        language="en",
        language_probability=0.99,
        duration_seconds=60.0,
    )


def _make_short_transcript():
    """Build a Transcript with fewer than 5 words."""
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="Hi bye")],
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


@pytest.fixture
def app_with_mocked_api(tmp_config):
    """ContextRecall instance with a wired-up mock API server.

    Bug X6: most orchestrator tests construct ContextRecall without an
    _api_server, which short-circuits _persist_audio and _db_update to
    no-ops. Status-transition correctness (which calls write 'transcribing'
    vs 'complete' vs 'error') and silent DB write drops (Bug C3) are
    therefore invisible to the suite — C3 specifically wasn't catchable
    until tests were written for it.

    This fixture wires:
      - app._api_server: a MagicMock with repo + a non-closed loop, so
        _db_update doesn't bail out at the "no api_server" gate.
      - app._persist_audio: stubbed to a deterministic meeting_id, so
        tests don't have to mock asyncio.run_coroutine_threadsafe.
      - app._db_update: replaced with a MagicMock spy so every status
        write is introspectable.
    """
    from src.main import ContextRecall
    from src.pipeline_runner import PipelineRunner

    patches = [
        patch("src.main.AudioCapture"),
        patch("src.main.TeamsDetector"),
        patch("src.main.Transcriber"),
        patch("src.main.Summariser"),
        # Suppress post-processing: the mocked loop never awaits the
        # runner's _post_process_async coroutine, which would produce a
        # RuntimeWarning at gc time. None of the X6 tests are about
        # post-processing behaviour.
        patch.object(PipelineRunner, "_dispatch_post_processing", MagicMock()),
    ]
    for p in patches:
        p.start()
    try:
        app = ContextRecall(config_path=tmp_config)

        mock_repo = MagicMock()
        mock_loop = MagicMock()
        mock_loop.is_closed.return_value = False
        mock_server = MagicMock()
        mock_server.repo = mock_repo
        mock_server.loop = mock_loop
        mock_server.db = MagicMock()
        app._api_server = mock_server

        # _persist_audio is exercised in its own dedicated tests; here we
        # want the focus on what comes after — what _db_update is called
        # with as the pipeline progresses.
        app._persist_audio = MagicMock(return_value=(Path("/tmp/audio.wav"), "test-meeting-id"))
        app._db_update = MagicMock()

        yield app
    finally:
        for p in patches:
            p.stop()


def _statuses_written(app) -> list[str]:
    """Return every status= value passed to _db_update, in call order."""
    return [
        call.kwargs.get("status")
        for call in app._db_update.call_args_list
        if "status" in call.kwargs
    ]


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_transcription_failure_does_not_crash_pipeline(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.side_effect = RuntimeError("Transcription exploded")

    # Should not raise.
    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    app._transcriber.transcribe.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_summarisation_failure_does_not_crash_pipeline(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.return_value = _make_transcript()
    app._summariser.summarise.side_effect = RuntimeError("Summarisation exploded")

    # Should not raise.
    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    app._summariser.summarise.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_short_transcript_skips_summarisation(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.return_value = _make_short_transcript()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    # Summariser should NOT have been called.
    app._summariser.summarise.assert_not_called()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_diarisation_conditional_execution(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_path,
    audio_file,
):
    from src.main import ContextRecall

    # Enable diarisation in config.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {"sample_rate": 16000, "temp_audio_dir": str(tmp_path / "audio")},
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": False},
        "notion": {"enabled": False},
        "diarisation": {"enabled": True},
        "api": {"enabled": False},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "test.log")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    with patch("src.main.create_diariser") as mock_factory:
        mock_diariser = MagicMock(spec=["diarise"])
        mock_factory.return_value = mock_diariser

        app = ContextRecall(config_path=config_path)
        transcript = _make_transcript()
        app._transcriber.transcribe.return_value = transcript
        # Diariser.diarise returns the (mutated) transcript.
        mock_diariser.diarise.return_value = transcript
        app._summariser.summarise.return_value = _make_summary()

        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        mock_diariser.diarise.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_markdown_writer_conditional_execution(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_path,
    audio_file,
):
    from src.main import ContextRecall

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {"sample_rate": 16000, "temp_audio_dir": str(tmp_path / "audio")},
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": True, "vault_path": str(tmp_path / "vault")},
        "notion": {"enabled": False},
        "diarisation": {"enabled": False},
        "api": {"enabled": False},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "test.log")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    with patch("src.main.MarkdownWriter") as mock_md_cls:
        mock_md_writer = MagicMock()
        mock_md_cls.return_value = mock_md_writer

        app = ContextRecall(config_path=config_path)
        app._transcriber.transcribe.return_value = _make_transcript()
        app._summariser.summarise.return_value = _make_summary()

        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        mock_md_writer.write.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_notion_writer_failure_isolated(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_path,
    audio_file,
):
    from src.main import ContextRecall

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {"sample_rate": 16000, "temp_audio_dir": str(tmp_path / "audio")},
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": True, "vault_path": str(tmp_path / "vault")},
        "notion": {"enabled": True, "api_key": "fake", "database_id": "fake-db"},
        "diarisation": {"enabled": False},
        "api": {"enabled": False},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "test.log")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    with (
        patch("src.main.MarkdownWriter") as mock_md_cls,
        patch("src.main.NotionWriter") as mock_notion_cls,
    ):
        mock_md_writer = MagicMock()
        mock_md_cls.return_value = mock_md_writer
        mock_notion_writer = MagicMock()
        mock_notion_writer.write.side_effect = RuntimeError("Notion API down")
        mock_notion_cls.return_value = mock_notion_writer

        app = ContextRecall(config_path=config_path)
        app._transcriber.transcribe.return_value = _make_transcript()
        app._summariser.summarise.return_value = _make_summary()

        # Should not raise despite Notion failure.
        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        # Notion was called and failed.
        mock_notion_writer.write.assert_called_once()
        # Markdown was still called (isolated from Notion failure).
        mock_md_writer.write.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_writer_last_error_emits_pipeline_warning(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_path,
    audio_file,
):
    """When a writer returns with last_error set, the orchestrator emits
    pipeline.warning so the UI can surface 'Markdown/Notion output skipped'.
    """
    from src.main import ContextRecall

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {"sample_rate": 16000, "temp_audio_dir": str(tmp_path / "audio")},
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": True, "vault_path": str(tmp_path / "vault")},
        "notion": {"enabled": True, "api_key": "fake", "database_id": "fake-db"},
        "diarisation": {"enabled": False},
        "api": {"enabled": False},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "test.log")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(config))

    with (
        patch("src.main.MarkdownWriter") as mock_md_cls,
        patch("src.main.NotionWriter") as mock_notion_cls,
    ):
        mock_md_writer = MagicMock()
        mock_md_writer.write.return_value = None
        mock_md_writer.last_error = "disk full"
        mock_md_cls.return_value = mock_md_writer

        mock_notion_writer = MagicMock()
        mock_notion_writer.write.return_value = None
        mock_notion_writer.last_error = "401 unauthorized"
        mock_notion_cls.return_value = mock_notion_writer

        app = ContextRecall(config_path=config_path)
        app._transcriber.transcribe.return_value = _make_transcript()
        app._summariser.summarise.return_value = _make_summary()

        emitted = []
        app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        warnings = [(t, k) for (t, k) in emitted if t == "pipeline.warning"]
        sources = {k.get("source") for (_, k) in warnings}
        assert "markdown" in sources
        assert "notion" in sources

        md_warning = next(k for (_, k) in warnings if k.get("source") == "markdown")
        notion_warning = next(k for (_, k) in warnings if k.get("source") == "notion")
        assert md_warning["message"] == "disk full"
        assert notion_warning["message"] == "401 unauthorized"


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_audio_persistence_fallback_to_copy(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    tmp_path,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.return_value = _make_transcript()
    app._summariser.summarise.return_value = _make_summary()

    # Set up a fake API server with repo so the persistence code path runs.
    mock_server = MagicMock()
    mock_repo = MagicMock()
    mock_loop = MagicMock()
    mock_loop.is_closed.return_value = False

    mock_future = MagicMock()
    mock_future.result.return_value = "test-meeting-id"

    mock_server.repo = mock_repo
    mock_server.loop = mock_loop
    app._api_server = mock_server

    # Create source audio file.
    audio_file = tmp_path / "source.wav"
    audio_file.write_bytes(b"RIFF" + b"\x00" * 40)

    from src.pipeline_runner import PipelineRunner

    # Suppress post-processing: the patched run_coroutine_threadsafe
    # never awaits the runner's coroutine, which produces a
    # RuntimeWarning at gc time. This test is about audio persistence.
    with (
        patch.object(PipelineRunner, "_dispatch_post_processing"),
        patch("asyncio.run_coroutine_threadsafe", return_value=mock_future),
    ):
        with patch("os.link", side_effect=OSError("cross-device link")):
            with patch("shutil.copy2") as mock_copy:
                app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)
                mock_copy.assert_called_once()


# ---------------------------------------------------------------------------
# Bug C3: silent _db_update on closed event loop
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_db_update_logs_error_when_event_loop_is_closed(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    caplog,
):
    """Reproduce Bug C3: when the API event loop has been torn down (UI
    closed mid-pipeline, daemon shutting down), _db_update silently drops
    the status update. The pipeline thread continues to "completion" but
    the meeting stays in 'transcribing' forever, with no log line to
    explain why the row never advanced.

    The fix surfaces a logger.error including the meeting id and the
    fields that were dropped so on-call can grep for it.
    """
    import logging

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    mock_server = MagicMock()
    mock_repo = MagicMock()
    mock_loop = MagicMock()
    mock_loop.is_closed.return_value = True  # the bug condition

    mock_server.repo = mock_repo
    mock_server.loop = mock_loop
    app._api_server = mock_server

    with caplog.at_level(logging.ERROR, logger="contextrecall"):
        app._db_update("meeting-123", status="error", title="X")

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, (
        "expected an ERROR log when scheduling a DB update on a closed loop; "
        "instead the function returned silently and the meeting will stay "
        "in its previous transient status forever"
    )
    combined = " ".join(r.getMessage() for r in error_records)
    assert "meeting-123" in combined, (
        "log must include the meeting id so on-call can correlate stuck rows"
    )


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_db_update_silent_when_no_api_server(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    caplog,
):
    """Counterpart: when there is no api_server at all (test mode, headless
    daemon), _db_update must remain silent. Only an actively-broken loop
    is an error worth logging."""
    import logging

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._api_server = None  # no API at all

    with caplog.at_level(logging.ERROR, logger="contextrecall"):
        app._db_update("meeting-123", status="error")

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records, "no api_server is a legitimate runtime mode and must not log an error"


# ---------------------------------------------------------------------------
# Bug B1: short-but-non-empty transcripts must not be marked errored
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_short_transcript_persists_as_complete_not_error(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    """Bug B1: a real but very short transcript ("hi bye thanks") was
    being marked 'error' just because it had < 5 words. That conflated
    "no audio at all" with "very short conversation" — losing the
    transcript the user actually got. The fix preserves the transcript
    and skips summarisation, but does NOT mark the meeting errored."""
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.return_value = _make_short_transcript()  # 2 words

    # Replace _persist_audio so we don't need a real DB; return a known id.
    app._persist_audio = MagicMock(return_value=(audio_file, "meet-short"))
    # Spy on _db_update so we can introspect every status write.
    app._db_update = MagicMock()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    # Summariser must NOT be called for trivial transcripts (Ollama would
    # generate garbage from 2 words).
    app._summariser.summarise.assert_not_called()

    # The meeting must NOT be marked 'error' just for being short.
    error_calls = [c for c in app._db_update.call_args_list if c.kwargs.get("status") == "error"]
    assert not error_calls, (
        f"short-but-non-empty transcripts must not be flagged as failed; got: {error_calls}"
    )

    # The transcript must be persisted so the user can see what they got.
    transcript_calls = [c for c in app._db_update.call_args_list if "transcript_json" in c.kwargs]
    assert transcript_calls, (
        "transcript_json must be persisted for short transcripts so the "
        "user can review the captured content"
    )


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_empty_transcript_still_marks_meeting_errored(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    """Counterpart: a truly empty transcript (no segments at all) is a
    legitimate failure and must still be flagged 'error'. Capture really
    did fail to produce usable audio."""
    from src.main import ContextRecall
    from src.transcriber import Transcript

    app = ContextRecall(config_path=tmp_config)
    app._transcriber.transcribe.return_value = Transcript(
        segments=[], language="en", language_probability=0.0, duration_seconds=0.0
    )

    app._persist_audio = MagicMock(return_value=(audio_file, "meet-empty"))
    app._db_update = MagicMock()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    app._summariser.summarise.assert_not_called()

    error_calls = [c for c in app._db_update.call_args_list if c.kwargs.get("status") == "error"]
    assert error_calls, "empty transcript must be flagged as error"


# ---------------------------------------------------------------------------
# Bug A4: orchestrator must emit pipeline.warning when capture warns
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_emits_pipeline_warning_when_capture_warns(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Bug A4: when AudioCapture's start() degraded silently to system-only
    (no default mic, configured mic missing), the user got no UI signal.
    The orchestrator must read capture.last_warning after start() and emit
    a pipeline.warning event so the existing UI banner (from A1) renders."""
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    # Pretend the capture layer surfaced a mic warning during start().
    app._capture.last_warning = (
        "Configured microphone 'USB Mic' was not found. Recording system audio only."
    )
    app._capture.start = MagicMock()

    app._emit = MagicMock()
    app._capture.is_recording = False
    app.api_start_recording()

    warning_calls = [
        c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.warning"
    ]
    assert warning_calls, (
        "orchestrator must emit pipeline.warning when capture.last_warning is set; "
        f"emitted: {[c.args for c in app._emit.call_args_list]}"
    )
    call = warning_calls[0]
    assert call.kwargs.get("source") == "mic"
    assert "USB Mic" in call.kwargs.get("message", "")


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_no_warning_emitted_on_clean_start(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Counterpart: a clean start (mic resolved, no degraded paths) must
    NOT emit a pipeline.warning — otherwise the banner would flash on
    every recording."""
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.last_warning = None
    app._capture.start = MagicMock()

    app._emit = MagicMock()
    app._capture.is_recording = False
    app.api_start_recording()

    warning_calls = [
        c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.warning"
    ]
    assert not warning_calls, f"clean start must not emit pipeline.warning; got: {warning_calls}"


# ---------------------------------------------------------------------------
# I1: the silent-system warning message must be backend-aware. Under the
# ScreenCaptureKit backend the fix is Screen Recording, not BlackHole routing.
# ---------------------------------------------------------------------------


def _fire_silent_input_warning(app):
    """Drive the wired audio.level callback once with the silence detector
    forced to fire, returning every emitted event as (type, kwargs)."""
    app._silent_input_detector = MagicMock()
    app._silent_input_detector.observe.return_value = True

    emitted: list[tuple] = []
    app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

    app._wire_audio_level_callback()
    app._capture.on_audio_level(system_rms=0.0, mic_rms=0.5)
    return emitted


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_silent_input_message_is_screen_recording_worded_for_sck_backend(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.main import ContextRecall
    from src.system_audio import ScreenCaptureKitSystemCapture
    from src.utils.config import AudioConfig

    app = ContextRecall(config_path=tmp_config)
    app._capture._system_backend = ScreenCaptureKitSystemCapture(
        AudioConfig(), Path("/nonexistent/helper")
    )

    emitted = _fire_silent_input_warning(app)

    silent = [
        k for (t, k) in emitted if t == "pipeline.warning" and k.get("type") == "silent_input"
    ]
    assert silent, f"SCK silence must emit a silent_input warning; got {emitted}"
    message = silent[0]["message"]
    assert "Screen Recording" in message, message
    assert "BlackHole" not in message, message
    assert silent[0]["source"] == "system"


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_silent_input_message_is_blackhole_worded_for_blackhole_backend(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.main import ContextRecall
    from src.system_audio import BlackHoleSystemCapture
    from src.utils.config import AudioConfig

    app = ContextRecall(config_path=tmp_config)
    app._capture._system_backend = BlackHoleSystemCapture(AudioConfig())

    emitted = _fire_silent_input_warning(app)

    silent = [
        k for (t, k) in emitted if t == "pipeline.warning" and k.get("type") == "silent_input"
    ]
    assert silent, f"BlackHole silence must emit a silent_input warning; got {emitted}"
    message = silent[0]["message"]
    assert "BlackHole" in message, message
    assert "Multi-Output Device" in message, message
    assert "Screen Recording" not in message, message


# ---------------------------------------------------------------------------
# I2: a capture warning that appears AFTER start() (the SCK "grant Screen
# Recording" hint, set at the end of the capture loop) must be surfaced once
# the merge completes — and a start-time mic warning must NOT be re-emitted.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_late_capture_warning_surfaces_sck_warning_when_none_at_start(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    emitted: list[tuple] = []
    app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

    # Start-time: no warning yet.
    app._capture.last_warning = None
    app._emit_capture_warnings()

    # The SCK backend records its hint at the end of the capture loop.
    sck_warning = (
        "System audio capture failed — grant Screen Recording in System "
        "Settings → Privacy & Security → Screen Recording, then re-record."
    )
    app._capture.last_warning = sck_warning
    app._emit_late_capture_warning()

    warnings = [(t, k) for (t, k) in emitted if t == "pipeline.warning"]
    assert len(warnings) == 1, f"exactly one warning expected; got {warnings}"
    assert warnings[0][1]["source"] == "system"
    assert "Screen Recording" in warnings[0][1]["message"]


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_late_capture_warning_does_not_double_emit_start_time_warning(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    emitted: list[tuple] = []
    app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

    # A mic warning surfaced at start and was already emitted. AudioCapture
    # only records last_warning once, so it is unchanged at the post-merge
    # point — the late emit must recognise it as already-surfaced.
    mic_warning = "Configured microphone 'USB Mic' was not found. Recording system audio only."
    app._capture.last_warning = mic_warning
    app._emit_capture_warnings()
    app._emit_late_capture_warning()

    warnings = [(t, k) for (t, k) in emitted if t == "pipeline.warning"]
    assert len(warnings) == 1, f"start-time warning must not be re-emitted; got {warnings}"
    assert warnings[0][1]["source"] == "mic"


def test_process_audio_emits_late_capture_warning_after_merge(app_with_mocked_api, audio_file):
    """Integration: _process_audio surfaces a post-merge SCK warning via the
    same pipeline.warning bus once wait_for_merge returns."""
    app = app_with_mocked_api

    emitted: list[tuple] = []
    app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

    sck_warning = (
        "System audio capture failed — grant Screen Recording in System "
        "Settings → Privacy & Security → Screen Recording, then re-record."
    )
    app._capture.merge_pending = True
    app._capture.wait_for_merge = MagicMock(return_value=True)
    app._capture.last_warning = sck_warning
    app._capture.last_error = None
    # Empty transcript short-circuits the heavy pipeline to an error write,
    # but only AFTER the post-merge warning emit we care about.
    app._transcriber.transcribe.return_value = Transcript(
        segments=[], language="en", language_probability=0.0, duration_seconds=0.0
    )

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    system_warnings = [
        k for (t, k) in emitted if t == "pipeline.warning" and k.get("source") == "system"
    ]
    assert system_warnings, f"post-merge SCK warning must be emitted; got {emitted}"
    assert "Screen Recording" in system_warnings[0]["message"]


# ---------------------------------------------------------------------------
# Unit 1: orchestrator must wire on_capture_error and on_stream_status
# BEFORE calling _capture.start(), and the callbacks must translate to
# pipeline.error / pipeline.warning events on the WebSocket bus.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_wires_capture_error_callback_before_start(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """The orchestrator must assign on_capture_error on the capture object
    BEFORE start() is invoked, otherwise a fast-failing start could fire
    its error callback into a None and the UI would never learn."""
    from src.audio_capture import AudioCaptureError
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    callback_at_start_call: dict[str, object] = {}

    def fake_start():
        callback_at_start_call["on_capture_error"] = app._capture.on_capture_error
        callback_at_start_call["on_stream_status"] = app._capture.on_stream_status

    app._capture.last_warning = None
    app._capture.start = fake_start

    app._emit = MagicMock()
    app._capture.is_recording = False
    app.api_start_recording()

    assert callable(callback_at_start_call.get("on_capture_error"))
    assert callable(callback_at_start_call.get("on_stream_status"))

    # Invoke the callback and verify it lands on the event bus as pipeline.error.
    app._emit.reset_mock()
    callback_at_start_call["on_capture_error"](AudioCaptureError("disconnect"))
    error_calls = [c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.error"]
    assert error_calls, "on_capture_error must emit pipeline.error"
    assert error_calls[0].kwargs.get("stage") == "capture"
    assert "disconnect" in error_calls[0].kwargs.get("error", "")

    # And on_stream_status must surface as pipeline.warning with source.
    app._emit.reset_mock()
    callback_at_start_call["on_stream_status"]("system", "input overflow")
    warning_calls = [
        c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.warning"
    ]
    assert warning_calls, "on_stream_status must emit pipeline.warning"
    assert warning_calls[0].kwargs.get("source") == "system"
    assert "input overflow" in warning_calls[0].kwargs.get("message", "")


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_wires_capture_error_callback_before_start(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Same wiring guarantee on the detector-driven entry point."""
    from src.audio_preflight import PreflightReport
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    callback_at_start_call: dict[str, object] = {}

    def fake_start():
        callback_at_start_call["on_capture_error"] = app._capture.on_capture_error
        callback_at_start_call["on_stream_status"] = app._capture.on_stream_status

    app._capture.last_warning = None
    app._capture.start = fake_start

    # Stub the pre-flight check with a healthy report: the real one probes
    # host audio devices and aborts meeting start on machines without
    # BlackHole (CI runners).
    with patch(
        "src.main.run_preflight",
        return_value=PreflightReport(
            blackhole_present=True,
            mic_openable=True,
            microphone_permission_likely=True,
        ),
    ):
        app._on_meeting_start(MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0))

    assert callable(callback_at_start_call.get("on_capture_error"))
    assert callable(callback_at_start_call.get("on_stream_status"))


# ---------------------------------------------------------------------------
# Bug X6: status-transition coverage using the shared mocked-API fixture.
# These tests exercise the _db_update path that the legacy tests above
# short-circuit by leaving _api_server unset. Pre-X6, a regression that
# stopped writing status='error' on a failure (or wrote it on the happy
# path) would slip through the suite — these tests close that gap.
# ---------------------------------------------------------------------------


def test_happy_path_writes_status_complete(app_with_mocked_api, audio_file):
    """Full pipeline must write status='complete' (with transcript_json
    and summary_markdown) and must not write status='error'."""
    app = app_with_mocked_api
    app._transcriber.transcribe.return_value = _make_transcript()
    app._summariser.summarise.return_value = _make_summary()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    statuses = _statuses_written(app)
    assert "complete" in statuses, f"happy path must mark meeting 'complete'; got {statuses}"
    assert "error" not in statuses, f"happy path must not mark meeting 'error'; got {statuses}"

    complete_calls = [
        call for call in app._db_update.call_args_list if call.kwargs.get("status") == "complete"
    ]
    assert any("transcript_json" in c.kwargs for c in complete_calls), (
        "complete write must persist the transcript_json"
    )
    assert any("summary_markdown" in c.kwargs for c in complete_calls), (
        "complete write must persist the summary_markdown"
    )


def test_transcription_failure_writes_status_error(app_with_mocked_api, audio_file):
    """Transcriber raises → meeting row must be moved to status='error'.
    Previously test_transcription_failure_does_not_crash_pipeline only
    asserted no-crash; it did not verify the row was actually marked
    errored on the way out."""
    app = app_with_mocked_api
    app._transcriber.transcribe.side_effect = RuntimeError("MLX exploded")

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    statuses = _statuses_written(app)
    assert "error" in statuses, f"transcription failure must mark meeting 'error'; got {statuses}"
    assert "complete" not in statuses, (
        f"transcription failure must not mark meeting 'complete'; got {statuses}"
    )


def test_summarisation_failure_writes_status_error(app_with_mocked_api, audio_file):
    """Summariser raises → meeting row must be moved to status='error'.
    Previously test_summarisation_failure_does_not_crash_pipeline only
    asserted no-crash; the row could silently stay in 'transcribing'
    forever if the orchestrator stopped calling _db_update."""
    app = app_with_mocked_api
    app._transcriber.transcribe.return_value = _make_transcript()
    app._summariser.summarise.side_effect = RuntimeError("Ollama timeout")

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    statuses = _statuses_written(app)
    assert "error" in statuses, f"summarisation failure must mark meeting 'error'; got {statuses}"
    assert "complete" not in statuses, (
        f"summarisation failure must not mark meeting 'complete'; got {statuses}"
    )


def test_empty_transcript_writes_status_error_via_api_path(app_with_mocked_api, audio_file):
    """An empty transcript (no segments) must mark the row 'error'. This
    is the API-path counterpart of test_empty_transcript_still_marks_meeting_errored
    above — exercised through the fixture so the failure mode would be
    caught even if _db_update wiring changed."""
    app = app_with_mocked_api
    app._transcriber.transcribe.return_value = Transcript(
        segments=[], language="en", language_probability=0.0, duration_seconds=0.0
    )

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    statuses = _statuses_written(app)
    assert "error" in statuses
    assert "complete" not in statuses
    app._summariser.summarise.assert_not_called()


def test_short_transcript_writes_status_complete_via_api_path(app_with_mocked_api, audio_file):
    """Short-but-non-empty transcript (Bug B1) must mark 'complete' and
    persist transcript_json — and must NOT call the summariser. This
    locks in the B1 contract on the API path."""
    app = app_with_mocked_api
    app._transcriber.transcribe.return_value = Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="hi bye")],
        language="en",
        language_probability=0.99,
        duration_seconds=2.0,
    )

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    statuses = _statuses_written(app)
    assert "complete" in statuses
    assert "error" not in statuses

    complete_calls = [
        call for call in app._db_update.call_args_list if call.kwargs.get("status") == "complete"
    ]
    assert any("transcript_json" in c.kwargs for c in complete_calls)
    # Summarisation must be skipped for short transcripts so Ollama doesn't
    # generate garbage from 2 words.
    app._summariser.summarise.assert_not_called()


# ---------------------------------------------------------------------------
# Bug X4: _on_meeting_end must not block the detector callback thread on
# live_transcriber.stop() — which can join its worker thread for up to 30s.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_end_returns_quickly_when_live_transcriber_stop_is_slow(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Bug X4: live_transcriber.stop() joins its worker thread with up to a
    30s timeout. _on_meeting_end runs on the detector callback thread, so
    while that join is in flight the detector can't poll for new meetings.
    A back-to-back meeting (e.g. one ends and another starts within 30s)
    can be silently missed.

    The fix: dispatch the join to a daemon thread so _on_meeting_end returns
    immediately. The live transcriber's worker is already a daemon, so the
    background join is safe to outlive the callback.
    """
    import threading
    import time

    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    # Live transcriber whose stop() blocks long enough that a synchronous
    # call would be obviously slow.
    slow_lt = MagicMock()
    stop_entered = threading.Event()
    stop_completed = threading.Event()

    def _slow_stop():
        stop_entered.set()
        time.sleep(1.0)
        stop_completed.set()

    slow_lt.stop.side_effect = _slow_stop
    app._live_transcriber = slow_lt

    # _capture.stop must return None so _on_meeting_end early-exits and
    # the timing we measure is only the live-transcriber-stop overhead.
    app._capture.stop = MagicMock(return_value=None)

    event = MeetingEvent(
        state=MeetingState.IDLE,
        started_at=1000.0,
        ended_at=1060.0,
        duration_seconds=60.0,
    )

    t0 = time.monotonic()
    app._on_meeting_end(event)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.3, (
        f"_on_meeting_end blocked the detector thread for {elapsed:.2f}s; "
        "it must return quickly so back-to-back meetings aren't missed "
        "while live_transcriber.stop() joins its worker thread"
    )

    # The slow stop must still have been invoked (just off the detector
    # thread). Without this assertion the test could pass by skipping
    # the cleanup entirely.
    assert stop_entered.wait(timeout=2.0), (
        "live_transcriber.stop() must still be invoked — just on a "
        "background daemon thread, not the detector callback thread"
    )

    # References must be cleared synchronously so a fresh meeting doesn't
    # see stale state if it starts before the background join finishes.
    assert app._live_transcriber is None
    assert app._capture.on_audio_data is None

    # Wait for the background stop to actually finish so the test doesn't
    # leak a sleeping thread into the next test.
    assert stop_completed.wait(timeout=3.0)


# ---------------------------------------------------------------------------
# Pre-flight integration: _on_meeting_start should call run_preflight first.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_runs_preflight_before_capture(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """The pre-flight check must run BEFORE capture.start() so missing
    BlackHole / mic permission is surfaced as a pipeline event instead
    of producing an empty recording."""
    from src.audio_preflight import PreflightReport
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    emitted: list[dict] = []
    app._emit = lambda event_type, **kwargs: emitted.append({"type": event_type, **kwargs})

    clean_report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
    )

    app._capture.is_recording = False
    with patch("src.main.run_preflight", return_value=clean_report) as mock_pf:
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )
        # refresh=True: re-scan PortAudio's device table so a long-running
        # daemon sees devices added/removed since the process started.
        mock_pf.assert_called_once_with(app._config.audio, refresh=True)
        app._capture.start.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_aborts_when_preflight_reports_error(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """If the pre-flight reports errors (e.g. BlackHole missing), the
    orchestrator must abort the start: no capture, no live transcriber,
    but the pipeline.error must be visible to the UI."""
    from src.audio_preflight import PreflightReport
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    emitted: list[dict] = []
    app._emit = lambda event_type, **kwargs: emitted.append({"type": event_type, **kwargs})

    bad_report = PreflightReport(
        blackhole_present=False,
        errors=["BlackHole virtual audio driver is not installed."],
    )

    with patch("src.main.run_preflight", return_value=bad_report):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._capture.start.assert_not_called()
    error_events = [e for e in emitted if e["type"] == "pipeline.error"]
    assert error_events, "preflight errors must be emitted as pipeline.error"
    assert any("BlackHole" in str(e.get("error", "")) for e in error_events)


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_emits_preflight_warnings_but_continues(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Preflight warnings (mic permission denied, mic mismatch) should
    surface as pipeline.warning events but must NOT block capture —
    system audio recording can still proceed without the mic."""
    from src.audio_preflight import PreflightReport
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    emitted: list[dict] = []
    app._emit = lambda event_type, **kwargs: emitted.append({"type": event_type, **kwargs})

    report = PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=False,
        microphone_permission_likely=False,
        warnings=["Microphone permission likely denied."],
    )

    with patch("src.main.run_preflight", return_value=report):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._capture.start.assert_called_once()
    warning_events = [e for e in emitted if e["type"] == "pipeline.warning"]
    assert any("Microphone permission" in str(w.get("message", "")) for w in warning_events)


# ---------------------------------------------------------------------------
# Unit 18: _setup_logging must use RotatingFileHandler so a long-running
# daemon doesn't grow an unbounded log file.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_setup_logging_installs_rotating_file_handler(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """The daemon runs under launchd for weeks at a time. A plain FileHandler
    would grow forever; _setup_logging must wire a RotatingFileHandler with
    a bounded size and a small backup count.
    """
    import logging
    import logging.handlers

    from src.main import ContextRecall

    # Reset the root logger so the assertion sees this run's handlers, not
    # a previous test's pile-up (basicConfig is a no-op if root already has
    # handlers).
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for h in saved_handlers:
        root.removeHandler(h)
    try:
        ContextRecall(config_path=tmp_config)

        rotating = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert rotating, (
            "Expected a RotatingFileHandler on the root logger after "
            "_setup_logging — otherwise daemon logs grow without bound."
        )
        handler = rotating[0]
        assert handler.maxBytes == 10 * 1024 * 1024
        assert handler.backupCount == 5
    finally:
        # Restore the root logger so other tests see the harness's normal
        # configuration.
        for h in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# Unit 20: orchestrator robustness — bounded future queue + signal-safe
# shutdown handler.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_end_caps_concurrent_pipelines(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """If 16 in-flight processing futures have already accumulated (e.g.
    the pipeline thread is stuck on a network call), _on_meeting_end must
    refuse to submit another job rather than grow the queue unboundedly.

    It must also emit pipeline.error so the saturation surfaces in the UI
    instead of as a slow memory leak."""
    from concurrent.futures import Future

    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._live_transcriber = None
    app._capture.stop = MagicMock(return_value=Path("/tmp/audio.wav"))

    # Pretend 16 prior pipelines are still in flight (none done).
    stuck_futures = []
    for _ in range(16):
        fut = Future()
        # Future is "pending" — done() returns False.
        stuck_futures.append(fut)
    app._processing_futures = stuck_futures

    app._emit = MagicMock()

    event = MeetingEvent(
        state=MeetingState.IDLE,
        started_at=1000.0,
        ended_at=1060.0,
        duration_seconds=60.0,
    )

    # Make stop() return a path that "exists" so we get past the early-out.
    with patch.object(Path, "exists", return_value=True):
        with pytest.raises(RuntimeError, match="too many concurrent pipelines"):
            app._on_meeting_end(event)

    error_calls = [c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.error"]
    assert error_calls, "saturation must be surfaced as pipeline.error before raising"
    assert "too many concurrent pipelines" in error_calls[0].kwargs.get("error", "")

    # Cleanup: cancel the stub futures so executor shutdown is clean.
    for f in stuck_futures:
        f.cancel()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_end_prunes_done_futures_under_cap(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Completed futures must be pruned before the cap is enforced, so a
    long-running daemon with many historical (done) jobs doesn't trip the
    saturation guard on its 17th meeting of the session."""
    from concurrent.futures import Future

    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._live_transcriber = None
    app._capture.stop = MagicMock(return_value=Path("/tmp/audio.wav"))

    # 16 already-completed futures — they must be pruned, not block submit.
    done_futures = []
    for _ in range(16):
        fut = Future()
        fut.set_result(None)
        done_futures.append(fut)
    app._processing_futures = done_futures

    event = MeetingEvent(
        state=MeetingState.IDLE,
        started_at=1000.0,
        ended_at=1060.0,
        duration_seconds=60.0,
    )

    # Make the submitted pipeline a no-op so the test doesn't actually run it.
    app._process_audio = MagicMock()

    with patch.object(Path, "exists", return_value=True):
        # Must not raise — prior futures are all done() == True and pruned.
        app._on_meeting_end(event)

    # A new future was queued and the done ones were pruned.
    assert len(app._processing_futures) == 1
    assert not app._processing_futures[0].done() or app._process_audio.called


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_shutdown_watcher_stops_detector_and_capture(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """The shutdown watcher thread must call detector.stop() when the event
    fires, and also call capture.stop(blocking=False) if a recording is in
    flight. This is the safe alternative to running heavyweight cleanup
    inside a signal handler from arbitrary threads."""
    import threading as _threading

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = True
    app._capture.stop = MagicMock()
    app._detector.stop = MagicMock()

    t = _threading.Thread(target=app._shutdown_watcher, daemon=True)
    t.start()

    # Pre-condition: nothing called yet.
    assert not app._detector.stop.called

    app._shutdown_event.set()
    t.join(timeout=2.0)

    assert not t.is_alive(), "shutdown watcher must exit once event is set"
    app._detector.stop.assert_called_once()
    app._capture.stop.assert_called_once_with(blocking=False)


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_shutdown_watcher_skips_capture_stop_when_not_recording(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Counterpart: if no recording is in flight, the watcher must not
    touch capture.stop() — calling stop() on an idle capture would error."""
    import threading as _threading

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = False
    app._capture.stop = MagicMock()
    app._detector.stop = MagicMock()

    t = _threading.Thread(target=app._shutdown_watcher, daemon=True)
    t.start()
    app._shutdown_event.set()
    t.join(timeout=2.0)

    app._detector.stop.assert_called_once()
    app._capture.stop.assert_not_called()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_signal_handlers_are_idempotent(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """First SIGINT/SIGTERM delivery sets the shutdown event; second
    delivery restores the previous handler and re-raises the signal so
    the process can be killed even if graceful shutdown is wedged."""
    import signal as _signal

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    # Capture the handler that gets installed without actually swapping out
    # the real signal handlers on the test process.
    installed: dict[int, object] = {}

    def fake_signal(signum, handler):
        installed[signum] = handler
        return _signal.SIG_DFL  # pretend there was no prior handler

    with patch("src.main.signal.signal", side_effect=fake_signal):
        app._install_signal_handlers()

    assert _signal.SIGINT in installed
    assert _signal.SIGTERM in installed

    handler = installed[_signal.SIGINT]
    assert callable(handler)

    # First invocation: sets the event, does not re-raise.
    handler(_signal.SIGINT, None)
    assert app._shutdown_event.is_set()
    assert app._signal_handler_invocations == 1

    # Second invocation: restores the previous handler and sends the signal
    # to the process. Patch os.kill so the test process doesn't actually die.
    with patch("src.main.signal.signal", side_effect=fake_signal):
        with patch("src.main.os.kill") as mock_kill:
            handler(_signal.SIGINT, None)
            mock_kill.assert_called_once_with(os.getpid(), _signal.SIGINT)
    assert app._signal_handler_invocations == 2


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_install_signal_handlers_tolerates_non_main_thread(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """signal.signal raises ValueError off the main thread. The installer
    must catch that so unit-test contexts (and any future background-thread
    daemon entry) don't crash on startup."""
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    with patch("src.main.signal.signal", side_effect=ValueError("not main thread")):
        # Must not raise.
        app._install_signal_handlers()


# ---------------------------------------------------------------------------
# First-boot config materialisation
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_init_materialises_default_config(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_path,
):
    """A fresh install has no config.yaml; the daemon must write the
    defaults on first boot so the settings API has a real file to read
    and update, instead of warning 'No config found' every minute."""
    from src.main import ContextRecall

    config_path = tmp_path / "config.yaml"
    assert not config_path.exists()

    ContextRecall(config_path=config_path)

    assert config_path.exists()


# ---------------------------------------------------------------------------
# Automatic audio routing wiring
# ---------------------------------------------------------------------------


def _clean_preflight_report():
    from src.audio_preflight import PreflightReport

    return PreflightReport(
        blackhole_present=True,
        blackhole_input_candidates=["BlackHole 2ch"],
        mic_openable=True,
        microphone_permission_likely=True,
        default_input_index=0,
    )


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_meeting_start_ensures_audio_routing(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Recording must not depend on the user hand-building a Multi-Output
    Device: meeting start routes system audio into the loopback."""
    from src.audio_routing import RoutingResult
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = False
    app._audio_router = MagicMock()
    app._audio_router.ensure_routed.return_value = RoutingResult(changed=True, message="routed")

    with patch("src.main.run_preflight", return_value=_clean_preflight_report()):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._audio_router.ensure_routed.assert_called_once()
    app._capture.start.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_meeting_start_skips_routing_when_disabled(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = False
    app._config.audio.auto_route_system_audio = False
    app._audio_router = MagicMock()

    with patch("src.main.run_preflight", return_value=_clean_preflight_report()):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._audio_router.ensure_routed.assert_not_called()
    app._capture.start.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_routing_failure_warns_but_recording_continues(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.audio_routing import RoutingResult
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = False
    app._audio_router = MagicMock()
    app._audio_router.ensure_routed.return_value = RoutingResult(error="HAL says no")

    emitted: list[dict] = []
    app._emit = lambda event_type, **kwargs: emitted.append({"type": event_type, **kwargs})

    with patch("src.main.run_preflight", return_value=_clean_preflight_report()):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    warnings = [e for e in emitted if e["type"] == "pipeline.warning"]
    assert any("HAL says no" in str(e.get("message", "")) for e in warnings)
    app._capture.start.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_meeting_end_restores_routing_even_without_audio(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """restore() must run even when capture produced no audio file."""
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._audio_router = MagicMock()
    app._live_transcriber = None
    app._capture.stop.return_value = None

    app._on_meeting_end(
        MeetingEvent(state=MeetingState.IDLE, started_at=1000.0, duration_seconds=60.0)
    )

    app._audio_router.restore.assert_called_once()


# ---------------------------------------------------------------------------
# Microphone-permission gate: every recording start must be blocked with an
# actionable pipeline.error when the daemon has no TCC microphone grant.
# (2026-07-07: grant was bound to the old MeetingMind binary path, so all
# recordings were -100 dBFS silence, then -9986 failures after a reboot.)
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_aborts_when_mic_permission_denied(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    emitted: list[dict] = []
    app._emit = lambda event_type, **kwargs: emitted.append({"type": event_type, **kwargs})

    with patch(
        "src.main.ensure_microphone_access",
        return_value=("denied", "Microphone access is denied for the Context Recall daemon."),
    ):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._capture.start.assert_not_called()
    error_events = [e for e in emitted if e["type"] == "pipeline.error"]
    assert error_events, "permission denial must surface as pipeline.error"
    assert any(e.get("stage") == "permission" for e in error_events)
    assert any("denied" in str(e.get("error", "")) for e in error_events)


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_proceeds_when_mic_authorized(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._emit = MagicMock()
    app._capture.is_recording = False

    with (
        patch("src.main.ensure_microphone_access", return_value=("authorized", None)),
        patch("src.main.run_preflight", return_value=_clean_preflight_report()),
    ):
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )

    app._capture.start.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_raises_when_mic_permission_denied(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """The manual path must fail the HTTP request with the actionable
    message (the route surfaces the exception detail) and never open
    streams that are doomed to record silence."""
    from src.audio_capture import AudioCaptureError
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._emit = MagicMock()

    problem = "Microphone access is denied for the Context Recall daemon."
    with patch("src.main.ensure_microphone_access", return_value=("denied", problem)):
        with pytest.raises(AudioCaptureError, match="denied"):
            app._capture.is_recording = False
            app.api_start_recording()

    app._capture.start.assert_not_called()
    error_calls = [c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.error"]
    assert error_calls, "manual start must emit pipeline.error on permission denial"


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_proceeds_when_permission_unknown(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Introspection failure (status 'unknown') must not block recording."""
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._emit = MagicMock()

    with patch("src.main.ensure_microphone_access", return_value=("unknown", None)):
        app._capture.is_recording = False
        app.api_start_recording()

    app._capture.start.assert_called_once()


# ---------------------------------------------------------------------------
# Capture-thread failure must hand the output device back. When the capture
# thread dies (e.g. PortAudio -9986 at stream.start), nothing ever calls
# stop(): a subsequent manual stop 409s on "Not recording", so the restore
# in the stop paths never runs and the managed multi-output stays the user's
# default (observed live 2026-07-07, 18:21–18:22).
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_capture_error_callback_restores_routing(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.audio_capture import AudioCaptureError
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._audio_router = MagicMock()
    app._emit = MagicMock()

    app._wire_capture_error_callbacks()
    error_callback = app._capture.on_capture_error
    error_callback(
        AudioCaptureError(
            "Failed to capture audio: Error starting stream: "
            "Internal PortAudio error [PaErrorCode -9986]"
        )
    )

    app._audio_router.restore.assert_called_once()
    error_calls = [c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.error"]
    assert error_calls, "capture errors must still reach the UI as pipeline.error"


# ---------------------------------------------------------------------------
# --process mode: no capture session, so there is no merge to wait for.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_process_audio_skips_merge_wait_without_capture_session(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    audio_file,
):
    """--process mode feeds an existing file straight into
    _process_audio; the capture never ran, so its merge event can never
    fire. The old unconditional wait_for_merge(120) made every --process
    invocation stall two minutes and then skip processing entirely."""
    from src.main import ContextRecall

    class _NoSessionCapture:
        merge_pending = False

        def wait_for_merge(self, timeout):
            raise AssertionError("must not wait for a merge no capture session started")

    app = ContextRecall(config_path=tmp_config)
    app._capture = _NoSessionCapture()
    app._transcriber.transcribe.return_value = _make_short_transcript()
    app._persist_audio = MagicMock(return_value=(audio_file, "meet-process-mode"))
    app._db_update = MagicMock()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    app._transcriber.transcribe.assert_called_once()


def test_capture_merge_pending_lifecycle(tmp_path):
    """merge_pending: False before any session, True while a started
    session's merge is outstanding, False once the merge event fires."""
    from src.audio_capture import AudioCapture
    from src.utils.config import AudioConfig

    capture = AudioCapture(AudioConfig(temp_audio_dir=str(tmp_path)))
    assert capture.merge_pending is False

    with (
        patch.object(AudioCapture, "_record_loop"),
        patch.object(AudioCapture, "_find_default_input_device", return_value=None),
    ):
        capture.start()
    try:
        assert capture.merge_pending is True
        capture._merge_complete.set()
        assert capture.merge_pending is False
    finally:
        capture._recording = False
        if capture._thread and capture._thread.is_alive():
            capture._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Log hygiene
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_third_party_http_loggers_are_quietened(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """httpx/huggingface_hub log one INFO line per HTTP request; launchd
    captures stdout to a file nothing rotates (11 MB observed). They must
    run at WARNING."""
    import logging as _logging

    from src.main import ContextRecall

    ContextRecall(config_path=tmp_config)
    assert _logging.getLogger("httpx").level == _logging.WARNING
    assert _logging.getLogger("huggingface_hub").level == _logging.WARNING


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_persist_audio_failure_logs_exception_type(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
    tmp_path,
    caplog,
):
    """A TimeoutError has an empty str() — the live log showed
    'Failed to create meeting record: ' with no clue. Log the repr."""
    import logging as _logging

    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    mock_server = MagicMock()
    mock_server.repo = MagicMock()
    mock_server.loop = MagicMock()
    app._api_server = mock_server

    audio = tmp_path / "meeting_20260707_120000.wav"
    audio.write_bytes(b"\x00" * 64)

    failing_future = MagicMock()
    failing_future.result.side_effect = TimeoutError()

    with (
        patch("src.main.default_audio_dir", return_value=tmp_path / "durable"),
        patch("src.main.asyncio.run_coroutine_threadsafe", return_value=failing_future),
        caplog.at_level(_logging.WARNING, logger="contextrecall"),
    ):
        app._persist_audio(audio, started_at=123.0)

    assert "TimeoutError" in caplog.text


# ---------------------------------------------------------------------------
# Deferred stop ("Process Later") — the meeting row must never be left
# without its audio_path pointer, and failures must be loud, not silent.
# ---------------------------------------------------------------------------


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def _make_app_with_real_persist(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    mock_server = MagicMock()
    mock_server.repo = MagicMock()
    mock_server.loop = MagicMock()
    mock_server.loop.is_closed.return_value = False
    app._api_server = mock_server
    return app


def test_persist_audio_waits_for_audio_path_update(tmp_config, tmp_path):
    """The audio_path write must not be fire-and-forget: a row created
    without it can never be processed from the UI (canRetryMeeting hides
    the button) nor via /reprocess (400, no audio file)."""
    app = _make_app_with_real_persist(tmp_config=tmp_config)

    audio = tmp_path / "meeting_20260707_120000.wav"
    audio.write_bytes(b"\x00" * 64)

    create_future = MagicMock()
    create_future.result.return_value = "meet-1"
    update_future = MagicMock()

    with (
        patch("src.main.default_audio_dir", return_value=tmp_path / "durable"),
        patch(
            "src.main.asyncio.run_coroutine_threadsafe",
            side_effect=[create_future, update_future],
        ),
    ):
        _, meeting_id = app._persist_audio(audio, started_at=123.0)

    assert meeting_id == "meet-1"
    update_future.result.assert_called_once()


def test_persist_audio_update_failure_logs_error_with_meeting_id(tmp_config, tmp_path, caplog):
    """If the audio_path write fails the row is in the poison state —
    keep the meeting_id (the row exists) but log an ERROR naming it."""
    import logging as _logging

    app = _make_app_with_real_persist(tmp_config=tmp_config)

    audio = tmp_path / "meeting_20260707_120000.wav"
    audio.write_bytes(b"\x00" * 64)

    create_future = MagicMock()
    create_future.result.return_value = "meet-orphan"
    update_future = MagicMock()
    update_future.result.side_effect = TimeoutError()

    with (
        patch("src.main.default_audio_dir", return_value=tmp_path / "durable"),
        patch(
            "src.main.asyncio.run_coroutine_threadsafe",
            side_effect=[create_future, update_future],
        ),
        caplog.at_level(_logging.ERROR, logger="contextrecall"),
    ):
        _, meeting_id = app._persist_audio(audio, started_at=123.0)

    assert meeting_id == "meet-orphan"
    assert "meet-orphan" in caplog.text


def test_stop_deferred_raises_when_meeting_record_missing(app_with_mocked_api, audio_file):
    """A deferred stop that cannot create the meeting row must raise so
    the API returns 500 — not silently return '' while the UI toasts
    'Recording saved. Process it later from Meetings.'"""
    import time as _time

    from src.audio_capture import AudioCaptureError

    app = app_with_mocked_api
    app._capture.stop.return_value = audio_file
    app._meeting_started_at = _time.time() - 60
    app._persist_audio = MagicMock(return_value=(audio_file, None))

    with pytest.raises(AudioCaptureError):
        app.api_stop_recording_deferred()


def test_stop_deferred_returns_meeting_id_and_writes_duration(app_with_mocked_api, audio_file):
    import time as _time

    app = app_with_mocked_api
    app._capture.stop.return_value = audio_file
    app._meeting_started_at = _time.time() - 60
    app._persist_audio = MagicMock(return_value=(audio_file, "meet-deferred"))

    meeting_id = app.api_stop_recording_deferred()

    assert meeting_id == "meet-deferred"
    persist_call = app._persist_audio.call_args
    assert persist_call.kwargs.get("status") == "pending"
    db_call = app._db_update.call_args
    assert db_call.args[0] == "meet-deferred"
    assert db_call.kwargs["duration_seconds"] == pytest.approx(60, abs=5)
    assert "ended_at" in db_call.kwargs


def _auto_arm_config(tmp_path, *, enabled, import_enabled=True):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    config = {
        "audio": {"temp_audio_dir": str(tmp_path / "audio")},
        "api": {"enabled": False},
        "diarisation": {"enabled": False},
        "markdown": {"enabled": False},
        "notion": {"enabled": False},
        "calendar": {"import_enabled": import_enabled},
        "auto_arm": {"enabled": enabled},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "t.log")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


def test_auto_arm_wired_when_enabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=True))
    app._maybe_start_auto_arm()

    assert app._auto_arm is not None
    assert app._detector.on_tick == app._auto_arm.tick


def test_auto_arm_absent_when_disabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=False))
    default_hook = app._detector.on_tick
    app._maybe_start_auto_arm()

    assert app._auto_arm is None
    assert app._detector.on_tick is default_hook  # unchanged


def test_auto_arm_absent_when_calendar_import_disabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=True, import_enabled=False))
    app._maybe_start_auto_arm()

    assert app._auto_arm is None


def test_calendar_matcher_kept_when_not_yet_authorized(tmp_path):
    """Regression (I1): a matcher that is not available at construction
    (permission not granted yet) must NOT be permanently nulled — it
    self-heals via match() once the boot poller obtains the grant."""
    from src.main import ContextRecall

    # The conftest calendar guard makes EventKit invisible, so the matcher
    # constructs unavailable — exactly the not-yet-authorized boot shape.
    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=False))

    assert app._config.calendar.enabled is True  # default-on
    assert app._calendar_matcher is not None
    assert app._calendar_matcher.available is False


def test_ensure_audio_routing_emits_warning_on_router_error(app_with_mocked_api):
    """A router that reports the switch did not take effect must surface a
    pipeline.warning (source=routing), not fail silently (Bug #5)."""
    from src.audio_routing import RoutingResult

    app = app_with_mocked_api
    app._config.audio.auto_route_system_audio = True
    app._audio_router = MagicMock()
    app._audio_router.ensure_routed.return_value = RoutingResult(
        error="System audio routing did not take effect."
    )
    app._emit = MagicMock()

    app._ensure_audio_routing()

    routing_warnings = [
        c
        for c in app._emit.call_args_list
        if c.args and c.args[0] == "pipeline.warning" and c.kwargs.get("source") == "routing"
    ]
    assert routing_warnings


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_api_start_recording_rejects_when_already_recording(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    tmp_config,
):
    """Starting over an in-flight recording must fail loudly, not reset the
    meeting clock: AudioCapture.start() would no-op ("Already recording"),
    after which api_start_recording used to overwrite _meeting_started_at
    and emit a spurious meeting.started for the same capture."""
    from src.audio_capture import AudioCaptureError
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)
    app._capture.is_recording = True
    app._capture.start = MagicMock()
    app._emit = MagicMock()
    app._meeting_started_at = 1234.0

    with pytest.raises(AudioCaptureError, match="already in progress"):
        app.api_start_recording()

    app._capture.start.assert_not_called()
    assert app._meeting_started_at == 1234.0  # clock untouched
    started_events = [
        c for c in app._emit.call_args_list if c.args and c.args[0] == "meeting.started"
    ]
    assert not started_events
