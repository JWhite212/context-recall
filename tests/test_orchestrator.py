"""Tests for src/main.py — MeetingMind orchestrator with heavy mocking."""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment


@pytest.fixture
def tmp_config(tmp_path):
    """Create a minimal config.yaml for MeetingMind init."""
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


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_transcription_failure_does_not_crash_pipeline(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_config, audio_file,
):
    from src.main import MeetingMind

    app = MeetingMind(config_path=tmp_config)
    app._transcriber.transcribe.side_effect = RuntimeError("Transcription exploded")

    # Should not raise.
    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    app._transcriber.transcribe.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_summarisation_failure_does_not_crash_pipeline(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_config, audio_file,
):
    from src.main import MeetingMind

    app = MeetingMind(config_path=tmp_config)
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
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_config, audio_file,
):
    from src.main import MeetingMind

    app = MeetingMind(config_path=tmp_config)
    app._transcriber.transcribe.return_value = _make_short_transcript()

    app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

    # Summariser should NOT have been called.
    app._summariser.summarise.assert_not_called()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_diarisation_conditional_execution(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_path, audio_file,
):
    from src.main import MeetingMind

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

    with patch("src.main.Diariser") as mock_diariser_cls:
        mock_diariser = MagicMock()
        mock_diariser_cls.return_value = mock_diariser

        app = MeetingMind(config_path=config_path)
        transcript = _make_transcript()
        app._transcriber.transcribe.return_value = transcript
        # Diariser.diarise returns the (mutated) transcript.
        mock_diariser.diarise.return_value = transcript
        app._summariser.summarise.return_value = _make_summary()

        # Set source file paths on the capture mock.
        sys_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        sys_path.write_bytes(b"RIFF" + b"\x00" * 40)
        mic_path.write_bytes(b"RIFF" + b"\x00" * 40)
        app._capture.system_audio_path = sys_path
        app._capture.mic_audio_path = mic_path

        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        mock_diariser.diarise.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_markdown_writer_conditional_execution(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_path, audio_file,
):
    from src.main import MeetingMind

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

        app = MeetingMind(config_path=config_path)
        app._transcriber.transcribe.return_value = _make_transcript()
        app._summariser.summarise.return_value = _make_summary()

        app._process_audio(audio_file, started_at=1000.0, duration_seconds=60.0)

        mock_md_writer.write.assert_called_once()


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_notion_writer_failure_isolated(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_path, audio_file,
):
    from src.main import MeetingMind

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

    with patch("src.main.MarkdownWriter") as mock_md_cls, \
         patch("src.main.NotionWriter") as mock_notion_cls:
        mock_md_writer = MagicMock()
        mock_md_cls.return_value = mock_md_writer
        mock_notion_writer = MagicMock()
        mock_notion_writer.write.side_effect = RuntimeError("Notion API down")
        mock_notion_cls.return_value = mock_notion_writer

        app = MeetingMind(config_path=config_path)
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
def test_audio_persistence_fallback_to_copy(
    mock_capture_cls, mock_transcriber_cls, mock_detector_cls, mock_summariser_cls,
    tmp_config, tmp_path,
):
    from src.main import MeetingMind

    app = MeetingMind(config_path=tmp_config)
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
