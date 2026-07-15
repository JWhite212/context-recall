"""Tests for PyAnnoteDiariser (all pyannote dependencies mocked)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from src.diariser import DiarisationConfig
from src.pyannote_diariser import PyAnnoteDiariser
from src.transcriber import Transcript, TranscriptSegment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> DiarisationConfig:
    return DiarisationConfig(enabled=True, backend="pyannote", **kwargs)


def _make_transcript(segments: list[TranscriptSegment]) -> Transcript:
    duration = max((s.end for s in segments), default=0.0)
    return Transcript(segments=segments, language="en", duration_seconds=duration)


def _mock_turn(start: float, end: float) -> MagicMock:
    """Create a mock pyannote turn object with .start and .end."""
    turn = MagicMock()
    turn.start = start
    turn.end = end
    return turn


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pipeline():
    """Provide a mock pyannote Pipeline with controllable speaker turns.

    Yields (PyAnnoteDiariser_class, mock_pipeline_instance, mock_pipeline_cls).
    """
    mock_pl_instance = MagicMock()
    mock_pl_cls = MagicMock()
    mock_pl_cls.from_pretrained.return_value = mock_pl_instance

    mock_pyannote_audio = MagicMock()
    mock_pyannote_audio.Pipeline = mock_pl_cls

    with patch.dict(
        sys.modules,
        {
            "pyannote": MagicMock(),
            "pyannote.audio": mock_pyannote_audio,
        },
    ):
        from src.pyannote_diariser import PyAnnoteDiariser

        yield PyAnnoteDiariser, mock_pl_instance, mock_pl_cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPyAnnoteDiariser:
    def test_diarise_assigns_speakers_by_overlap(self, mock_pipeline):
        """Segments are assigned to the speaker with the largest temporal overlap."""
        PyAnnoteDiariser, mock_pl_instance, _ = mock_pipeline

        # Speaker 00 talks 0-5s, Speaker 01 talks 5-12s.
        mock_result = MagicMock()
        mock_result.itertracks.return_value = [
            (_mock_turn(0.0, 5.0), None, "SPEAKER_00"),
            (_mock_turn(5.0, 12.0), None, "SPEAKER_01"),
        ]
        mock_pl_instance.return_value = mock_result

        config = _make_config()
        diariser = PyAnnoteDiariser(config)

        transcript = _make_transcript(
            [
                TranscriptSegment(start=1.0, end=4.0, text="Hello there"),
                TranscriptSegment(start=6.0, end=10.0, text="Good morning"),
            ]
        )

        result = diariser.diarise(transcript, Path("/fake/audio.wav"))

        assert result.segments[0].speaker == "SPEAKER_00"
        assert result.segments[1].speaker == "SPEAKER_01"

    def test_diarise_no_overlap_falls_back_to_remote_label(self, mock_pipeline):
        """When no pipeline turns overlap a segment, it falls back to
        config.remote_label (the hybrid diariser's non-user fallback —
        previously this left the segment unlabelled)."""
        PyAnnoteDiariser, mock_pl_instance, _ = mock_pipeline

        # Pipeline says speech only at 100-110s.
        mock_result = MagicMock()
        mock_result.itertracks.return_value = [
            (_mock_turn(100.0, 110.0), None, "SPEAKER_00"),
        ]
        mock_pl_instance.return_value = mock_result

        config = _make_config()
        diariser = PyAnnoteDiariser(config)

        # Transcript segment is at 0-5s -- no overlap with 100-110s.
        transcript = _make_transcript(
            [
                TranscriptSegment(start=0.0, end=5.0, text="No overlap"),
            ]
        )

        result = diariser.diarise(transcript, Path("/fake/audio.wav"))

        assert result.segments[0].speaker == config.remote_label

    def test_num_speakers_passed_to_pipeline(self, mock_pipeline):
        """When num_speakers > 0, it is passed as a kwarg to the pipeline call."""
        PyAnnoteDiariser, mock_pl_instance, _ = mock_pipeline

        mock_result = MagicMock()
        mock_result.itertracks.return_value = []
        mock_pl_instance.return_value = mock_result

        config = _make_config(num_speakers=3)
        diariser = PyAnnoteDiariser(config)

        transcript = _make_transcript([])
        diariser.diarise(transcript, Path("/fake/audio.wav"))

        # The pipeline was called with num_speakers=3.
        _, kwargs = mock_pl_instance.call_args
        assert kwargs["num_speakers"] == 3

    def test_num_speakers_zero_not_passed(self, mock_pipeline):
        """When num_speakers == 0, it is NOT passed (auto-detect)."""
        PyAnnoteDiariser, mock_pl_instance, _ = mock_pipeline

        mock_result = MagicMock()
        mock_result.itertracks.return_value = []
        mock_pl_instance.return_value = mock_result

        config = _make_config(num_speakers=0)
        diariser = PyAnnoteDiariser(config)

        transcript = _make_transcript([])
        diariser.diarise(transcript, Path("/fake/audio.wav"))

        _, kwargs = mock_pl_instance.call_args
        assert "num_speakers" not in kwargs

    def test_lazy_loading(self, mock_pipeline):
        """Creating PyAnnoteDiariser does NOT load the pipeline."""
        PyAnnoteDiariser, _, mock_pl_cls = mock_pipeline

        config = _make_config()
        PyAnnoteDiariser(config)

        # from_pretrained should not have been called yet.
        mock_pl_cls.from_pretrained.assert_not_called()

    def test_pipeline_loaded_once(self, mock_pipeline):
        """Calling diarise() twice only calls from_pretrained() once."""
        PyAnnoteDiariser, mock_pl_instance, mock_pl_cls = mock_pipeline

        mock_result = MagicMock()
        mock_result.itertracks.return_value = []
        mock_pl_instance.return_value = mock_result

        config = _make_config()
        diariser = PyAnnoteDiariser(config)

        transcript1 = _make_transcript([])
        transcript2 = _make_transcript([])
        diariser.diarise(transcript1, Path("/fake/audio.wav"))
        diariser.diarise(transcript2, Path("/fake/audio.wav"))

        assert mock_pl_cls.from_pretrained.call_count == 1


# ---------------------------------------------------------------------------
# Hybrid diariser: energy me/remote pre-pass + pyannote remote separation.
# The pyannote pipeline is always mocked via a stubbed _speaker_turns — no
# real model is loaded.
# ---------------------------------------------------------------------------


def _wav(path: Path, loud: bool, seconds: float = 3.0, sr: int = 16000):
    n = int(seconds * sr)
    amp = 0.5 if loud else 0.0001
    sf.write(str(path), (np.random.randn(n) * amp).astype("float32"), sr)


def _transcript():
    return Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="hi", speaker=""),
            TranscriptSegment(start=1.0, end=2.0, text="hello there", speaker=""),
            TranscriptSegment(start=2.0, end=3.0, text="agreed", speaker=""),
        ],
        language="en",
    )


def _diariser_with_turns(turns):
    cfg = DiarisationConfig(backend="pyannote")
    d = PyAnnoteDiariser(cfg)
    # Stub the pyannote turn extraction — never load a real model.
    d._speaker_turns = lambda audio_path: turns  # type: ignore
    return d, cfg


def test_mic_dominant_segment_is_me(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=False)  # remote quiet
    _wav(mic, loud=True)  # user talking
    d, cfg = _diariser_with_turns([(0.0, 3.0, "SPEAKER_00")])
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert all(seg.speaker == cfg.speaker_name for seg in t.segments)


def test_remote_segments_get_pyannote_speakers(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=True)  # remote talking
    _wav(mic, loud=False)  # user quiet
    d, cfg = _diariser_with_turns([(0.0, 1.5, "SPEAKER_00"), (1.5, 3.0, "SPEAKER_01")])
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert t.segments[0].speaker == "SPEAKER_00"
    assert t.segments[2].speaker == "SPEAKER_01"
    assert cfg.speaker_name not in {s.speaker for s in t.segments}


def test_remote_segment_without_overlap_falls_back_to_remote_label(tmp_path):
    system = tmp_path / "m_system.wav"
    mic = tmp_path / "m_mic.wav"
    _wav(system, loud=True)
    _wav(mic, loud=False)
    d, cfg = _diariser_with_turns([])  # pyannote found no turns
    t = _transcript()
    d.diarise(t, system, mic_audio_path=mic, system_audio_path=system)
    assert all(seg.speaker == cfg.remote_label for seg in t.segments)
