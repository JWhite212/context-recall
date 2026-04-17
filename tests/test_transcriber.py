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


def _make_mlx_result():
    """Build a canned mlx_whisper.transcribe() return dict."""
    return {
        "text": "First segment. Second segment.",
        "segments": [
            {"id": 0, "start": 0.0, "end": 3.0, "text": "First segment."},
            {"id": 1, "start": 3.0, "end": 7.0, "text": "Second segment."},
        ],
        "language": "en",
    }


class TestTranscriber:
    """Verify Transcriber transcription behaviour with MLX Whisper."""

    def test_transcribe_file_not_found(self):
        config = TranscriptionConfig()
        transcriber = Transcriber(config)
        with pytest.raises(FileNotFoundError):
            transcriber.transcribe(Path("/nonexistent/audio.wav"))

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_on_segment_callback_error_resilience(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig()
        transcriber = Transcriber(config)

        mock_transcribe.return_value = _make_mlx_result()

        # Create a dummy audio file so the existence check passes.
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        # Callback that always raises.
        bad_callback = MagicMock(side_effect=ValueError("callback broke"))

        # Transcription should complete despite the broken callback.
        result = transcriber.transcribe(audio_file, on_segment=bad_callback)
        assert len(result.segments) == 2
        assert bad_callback.call_count == 2

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_transcribe_returns_transcript(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig()
        transcriber = Transcriber(config)

        mock_transcribe.return_value = _make_mlx_result()

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        result = transcriber.transcribe(audio_file)

        assert isinstance(result, Transcript)
        assert result.language == "en"
        assert result.language_probability == 0.0
        assert result.duration_seconds == 7.0
        assert len(result.segments) == 2
        assert result.segments[0].text == "First segment."
        assert result.segments[1].text == "Second segment."
        assert result.segments[0].start == 0.0
        assert result.segments[1].end == 7.0

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_transcribe_empty_segments(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig()
        transcriber = Transcriber(config)

        mock_transcribe.return_value = {"segments": [], "language": "en", "text": ""}

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        result = transcriber.transcribe(audio_file)
        assert len(result.segments) == 0
        assert result.duration_seconds == 0.0

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_transcribe_passes_model_and_language(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig(
            model_size="mlx-community/whisper-small.en",
            language="auto",
        )
        transcriber = Transcriber(config)

        mock_transcribe.return_value = _make_mlx_result()

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)

        transcriber.transcribe(audio_file)

        mock_transcribe.assert_called_once()
        call_kwargs = mock_transcribe.call_args
        assert call_kwargs.kwargs["path_or_hf_repo"] == "mlx-community/whisper-small.en"
        assert call_kwargs.kwargs["language"] is None  # "auto" maps to None

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_transcribe_passes_all_params(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig(
            compression_ratio_threshold=3.0,
            logprob_threshold=-0.5,
            no_speech_threshold=0.7,
            temperature=(0.0, 0.4, 0.8),
            initial_prompt="MeetingMind standup",
        )
        transcriber = Transcriber(config)
        mock_transcribe.return_value = _make_mlx_result()

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        transcriber.transcribe(audio_file)

        kw = mock_transcribe.call_args.kwargs
        assert kw["condition_on_previous_text"] is False
        assert kw["compression_ratio_threshold"] == 3.0
        assert kw["logprob_threshold"] == -0.5
        assert kw["no_speech_threshold"] == 0.7
        assert kw["hallucination_silence_threshold"] is None
        assert kw["temperature"] == (0.0, 0.4, 0.8)
        assert kw["initial_prompt"] == "MeetingMind standup"
        assert kw["verbose"] is False

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_empty_initial_prompt_maps_to_none(self, mock_transcribe, tmp_path):
        config = TranscriptionConfig(initial_prompt="")
        transcriber = Transcriber(config)
        mock_transcribe.return_value = _make_mlx_result()

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        transcriber.transcribe(audio_file)

        assert mock_transcribe.call_args.kwargs["initial_prompt"] is None

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_backward_timestamp_segments_filtered(self, mock_transcribe, tmp_path):
        """Segments that jump backwards in time should be dropped."""
        config = TranscriptionConfig()
        transcriber = Transcriber(config)
        mock_transcribe.return_value = {
            "segments": [
                {"id": 0, "start": 0.0, "end": 3.0, "text": "First."},
                {"id": 1, "start": 1.0, "end": 2.0, "text": "Backward."},
                {"id": 2, "start": 3.0, "end": 6.0, "text": "Third."},
            ],
            "language": "en",
        }

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        result = transcriber.transcribe(audio_file)

        assert len(result.segments) == 2
        assert result.segments[0].text == "First."
        assert result.segments[1].text == "Third."

    @patch("src.transcriber.mlx_whisper.transcribe")
    def test_repetition_hallucination_segments_filtered(self, mock_transcribe, tmp_path):
        """Segments with repeated-word hallucinations should be dropped."""
        config = TranscriptionConfig()
        transcriber = Transcriber(config)
        mock_transcribe.return_value = {
            "segments": [
                {"id": 0, "start": 0.0, "end": 3.0, "text": "Normal speech here."},
                {
                    "id": 1,
                    "start": 3.0,
                    "end": 6.0,
                    "text": "Dios Dios Dios Dios Dios Dios",
                },
                {"id": 2, "start": 6.0, "end": 9.0, "text": "Back to normal."},
            ],
            "language": "en",
        }

        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"\x00" * 100)
        result = transcriber.transcribe(audio_file)

        assert len(result.segments) == 2
        assert result.segments[0].text == "Normal speech here."
        assert result.segments[1].text == "Back to normal."


# ------------------------------------------------------------------
# Hallucination filter unit tests
# ------------------------------------------------------------------


class TestRepetitionHallucination:
    """Verify _is_repetition_hallucination static method."""

    def test_detects_repeated_words(self):
        assert Transcriber._is_repetition_hallucination("Dios Dios Dios Dios Dios Dios")

    def test_normal_text_passes(self):
        assert not Transcriber._is_repetition_hallucination("Hello how are you doing today")

    def test_short_repetition_allowed(self):
        # 3 consecutive repeats is below the default threshold of 5.
        assert not Transcriber._is_repetition_hallucination("yes yes yes done")

    def test_empty_string(self):
        assert not Transcriber._is_repetition_hallucination("")


class TestTextCompressionRatio:
    """Verify _text_compression_ratio static method."""

    def test_normal_text_low_ratio(self):
        ratio = Transcriber._text_compression_ratio("Hello, how are you doing today?")
        assert ratio < 15.0

    def test_repetitive_text_high_ratio(self):
        ratio = Transcriber._text_compression_ratio("ha " * 100)
        assert ratio > 15.0

    def test_empty_string_returns_zero(self):
        assert Transcriber._text_compression_ratio("") == 0.0
