"""Tests for speech-to-text transcription data structures and Transcriber."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.transcriber import Transcriber, Transcript, TranscriptSegment
from src.utils.config import TranscriptionConfig

# ------------------------------------------------------------------
# TranscriptSegment tests
# ------------------------------------------------------------------


class TestTranscriptSegment:
    """Verify timestamp formatting."""

    def test_timestamp_formats_hh_mm_ss(self):
        seg = TranscriptSegment(start=3661.0, end=3670.0, text="hello")
        assert seg.timestamp == "[01:01:01]"

    def test_timestamp_zero(self):
        seg = TranscriptSegment(start=0.0, end=1.0, text="start")
        assert seg.timestamp == "[00:00:00]"

    def test_timestamp_large_value(self):
        seg = TranscriptSegment(start=86400.0, end=86401.0, text="day")
        assert seg.timestamp == "[24:00:00]"

    def test_timestamp_fractional_seconds(self):
        seg = TranscriptSegment(start=3661.7, end=3670.0, text="hello")
        # int(3661.7) == 3661 → 1h 1m 1s (truncates, does not round).
        assert seg.timestamp == "[01:01:01]"


# ------------------------------------------------------------------
# Transcript tests
# ------------------------------------------------------------------


class TestTranscript:
    """Verify aggregation properties on Transcript."""

    def _make_segments(self):
        return [
            TranscriptSegment(start=0.0, end=3.0, text="Hello everyone."),
            TranscriptSegment(start=3.0, end=7.0, text="How are you?"),
            TranscriptSegment(start=7.0, end=12.0, text="Let's get started."),
        ]

    def test_full_text_concatenates_segments(self):
        transcript = Transcript(segments=self._make_segments())
        assert transcript.full_text == "Hello everyone. How are you? Let's get started."

    def test_full_text_empty_segments(self):
        transcript = Transcript(segments=[])
        assert transcript.full_text == ""

    def test_timestamped_text_without_speakers(self):
        transcript = Transcript(segments=self._make_segments())
        lines = transcript.timestamped_text.split("\n")
        assert lines[0] == "[00:00:00] Hello everyone."
        assert lines[1] == "[00:00:03] How are you?"
        assert lines[2] == "[00:00:07] Let's get started."

    def test_timestamped_text_with_speakers(self):
        segments = [
            TranscriptSegment(start=0.0, end=3.0, text="Hello.", speaker="Me"),
            TranscriptSegment(start=3.0, end=7.0, text="Hi.", speaker="Remote"),
        ]
        transcript = Transcript(segments=segments)
        lines = transcript.timestamped_text.split("\n")
        assert lines[0] == "[00:00:00] [Me] Hello."
        assert lines[1] == "[00:00:03] [Remote] Hi."

    def test_word_count(self):
        transcript = Transcript(segments=self._make_segments())
        # "Hello everyone." = 2, "How are you?" = 3, "Let's get started." = 3
        assert transcript.word_count == 8

    def test_word_count_cjk_no_spaces(self):
        """CJK text without spaces counts as one 'word' per segment (str.split behaviour)."""
        segments = [
            TranscriptSegment(start=0.0, end=5.0, text="\u4f1a\u8bae\u8ba8\u8bba"),
        ]
        transcript = Transcript(segments=segments)
        assert transcript.word_count == 1

    def test_timestamped_text_empty_text_segment(self):
        segments = [
            TranscriptSegment(start=0.0, end=1.0, text=""),
        ]
        transcript = Transcript(segments=segments)
        # f"{timestamp} {text.strip()}" produces a trailing space for empty text.
        assert transcript.timestamped_text == "[00:00:00] "

    def test_to_dict_round_trip(self):
        segments = self._make_segments()
        transcript = Transcript(
            segments=segments,
            language="en",
            language_probability=0.95,
            duration_seconds=12.0,
        )
        d = transcript.to_dict()
        assert d["language"] == "en"
        assert d["language_probability"] == 0.95
        assert d["duration_seconds"] == 12.0
        assert len(d["segments"]) == 3
        assert d["segments"][0]["start"] == 0.0
        assert d["segments"][0]["text"] == "Hello everyone."
        assert d["segments"][1]["speaker"] == ""


# ------------------------------------------------------------------
# Transcriber tests
# ------------------------------------------------------------------


def _make_mock_model():
    """Build a mock WhisperModel whose transcribe() returns canned data."""
    mock_model = MagicMock()

    seg1 = MagicMock()
    seg1.start = 0.0
    seg1.end = 3.0
    seg1.text = "First segment."

    seg2 = MagicMock()
    seg2.start = 3.0
    seg2.end = 7.0
    seg2.text = "Second segment."

    info = MagicMock()
    info.language = "en"
    info.language_probability = 0.97
    info.duration = 7.0

    mock_model.transcribe.return_value = ([seg1, seg2], info)
    return mock_model


class TestTranscriber:
    """Verify Transcriber model loading and transcription behaviour."""

    def test_transcribe_file_not_found(self):
        config = TranscriptionConfig(model_size="tiny.en")
        transcriber = Transcriber(config)
        with pytest.raises(FileNotFoundError):
            transcriber.transcribe(Path("/nonexistent/audio.wav"))

    @patch("src.transcriber.WhisperModel")
    def test_load_model_auto_becomes_int8(self, MockWhisperModel):
        config = TranscriptionConfig(model_size="tiny.en", compute_type="auto")
        transcriber = Transcriber(config)

        MockWhisperModel.return_value = _make_mock_model()
        transcriber._load_model()

        MockWhisperModel.assert_called_once()
        call_kwargs = MockWhisperModel.call_args
        assert call_kwargs.kwargs.get("compute_type") == "int8" or \
            call_kwargs[1].get("compute_type") == "int8"

    @patch("src.transcriber.WhisperModel")
    def test_load_model_lazy(self, MockWhisperModel):
        config = TranscriptionConfig(model_size="tiny.en")
        transcriber = Transcriber(config)

        # Model should not be loaded at construction time.
        assert transcriber._model is None
        MockWhisperModel.assert_not_called()

        # After calling _load_model, the model should be set.
        MockWhisperModel.return_value = MagicMock()
        transcriber._load_model()
        assert transcriber._model is not None
        MockWhisperModel.assert_called_once()

    @patch("src.transcriber.WhisperModel")
    def test_on_segment_callback_error_resilience(self, MockWhisperModel, tmp_path):
        config = TranscriptionConfig(model_size="tiny.en")
        transcriber = Transcriber(config)

        mock_model = _make_mock_model()
        MockWhisperModel.return_value = mock_model
        # Also make the Transcriber use our mock when _load_model is called.
        transcriber._model = mock_model

        # Create a dummy audio file so the existence check passes.
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        # Callback that always raises.
        bad_callback = MagicMock(side_effect=ValueError("callback broke"))

        # Transcription should complete despite the broken callback.
        result = transcriber.transcribe(audio_file, on_segment=bad_callback)
        assert len(result.segments) == 2
        assert bad_callback.call_count == 2

    @patch("src.transcriber.WhisperModel")
    def test_transcribe_returns_transcript(self, MockWhisperModel, tmp_path):
        config = TranscriptionConfig(model_size="tiny.en")
        transcriber = Transcriber(config)

        mock_model = _make_mock_model()
        MockWhisperModel.return_value = mock_model
        transcriber._model = mock_model

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        result = transcriber.transcribe(audio_file)

        assert isinstance(result, Transcript)
        assert result.language == "en"
        assert result.language_probability == 0.97
        assert result.duration_seconds == 7.0
        assert len(result.segments) == 2
        assert result.segments[0].text == "First segment."
        assert result.segments[1].text == "Second segment."
        assert result.segments[0].start == 0.0
        assert result.segments[1].end == 7.0
