"""Tests for src/main.py - Context Recall orchestrator with heavy mocking."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment


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

    patches = [
        patch("src.main.AudioCapture"),
        patch("src.main.TeamsDetector"),
        patch("src.main.Transcriber"),
        patch("src.main.Summariser"),
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

        # Suppress post-processing: _post_process_async is a coroutine the
        # mocked loop never awaits, which produces a RuntimeWarning at gc
        # time. None of the X6 tests are about post-processing behaviour.
        app._run_post_processing = MagicMock()

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

    with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
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
    app.api_start_recording()

    warning_calls = [
        c for c in app._emit.call_args_list if c.args and c.args[0] == "pipeline.warning"
    ]
    assert not warning_calls, f"clean start must not emit pipeline.warning; got: {warning_calls}"


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
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=tmp_config)

    callback_at_start_call: dict[str, object] = {}

    def fake_start():
        callback_at_start_call["on_capture_error"] = app._capture.on_capture_error
        callback_at_start_call["on_stream_status"] = app._capture.on_stream_status

    app._capture.last_warning = None
    app._capture.start = fake_start

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

    with patch("src.main.run_preflight", return_value=clean_report) as mock_pf:
        app._on_meeting_start(
            MeetingEvent(state=MeetingState.ACTIVE, started_at=1000.0, duration_seconds=0.0)
        )
        mock_pf.assert_called_once_with(app._config.audio)
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
