"""Tests for the Diariser and energy-based speaker labelling."""

import numpy as np
import pytest
import soundfile as sf

from src.diariser import DiarisationConfig, Diariser
from src.transcriber import Transcript, TranscriptSegment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wav(path, data, sr=16000):
    """Write a numpy array to a WAV file."""
    sf.write(str(path), data, sr, subtype="PCM_16")


def _sine_wave(duration_s=1.0, freq=440, sr=16000, amplitude=0.5):
    """Generate a sine wave at the given frequency."""
    t = np.linspace(0, 2 * np.pi * freq * duration_s, int(sr * duration_s))
    return (np.sin(t) * amplitude).astype(np.float32)


def _silence(duration_s=1.0, sr=16000):
    """Generate silence."""
    return np.zeros(int(sr * duration_s), dtype=np.float32)


def _make_diariser(**kwargs) -> Diariser:
    """Create a Diariser with default config, overridable via kwargs."""
    config = DiarisationConfig(enabled=True, **kwargs)
    return Diariser(config)


def _make_transcript(segments: list[TranscriptSegment]) -> Transcript:
    """Wrap segments in a Transcript."""
    duration = max((s.end for s in segments), default=0.0)
    return Transcript(
        segments=segments,
        language="en",
        language_probability=0.99,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDiariser:
    def test_missing_system_audio_raises(self, tmp_path):
        mic_path = tmp_path / "mic.wav"
        _make_wav(mic_path, _silence())
        system_path = tmp_path / "system.wav"  # Does not exist.

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="hello"),
        ])

        with pytest.raises(FileNotFoundError, match="system audio"):
            diariser.diarise(transcript, system_path, mic_path)

    def test_missing_mic_audio_raises(self, tmp_path):
        system_path = tmp_path / "system.wav"
        _make_wav(system_path, _silence())
        mic_path = tmp_path / "mic.wav"  # Does not exist.

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="hello"),
        ])

        with pytest.raises(FileNotFoundError, match="mic audio"):
            diariser.diarise(transcript, system_path, mic_path)

    def test_sample_rate_mismatch_raises(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        _make_wav(system_path, _silence(sr=16000), sr=16000)
        _make_wav(mic_path, _silence(sr=44100), sr=44100)

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="hello"),
        ])

        with pytest.raises(ValueError, match="Sample rate mismatch"):
            diariser.diarise(transcript, system_path, mic_path)

    def test_mic_louder_labels_me(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        _make_wav(system_path, _silence())
        _make_wav(mic_path, _sine_wave())

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="I am speaking"),
        ])

        result = diariser.diarise(transcript, system_path, mic_path)
        assert result.segments[0].speaker == "Me"

    def test_system_louder_labels_remote(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        _make_wav(system_path, _sine_wave())
        _make_wav(mic_path, _silence())

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="Remote speaking"),
        ])

        result = diariser.diarise(transcript, system_path, mic_path)
        assert result.segments[0].speaker == "Remote"

    def test_similar_energy_labels_both(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        # Same amplitude so neither exceeds the threshold ratio.
        _make_wav(system_path, _sine_wave(amplitude=0.5))
        _make_wav(mic_path, _sine_wave(amplitude=0.5))

        diariser = _make_diariser()
        transcript = _make_transcript([
            TranscriptSegment(start=0.0, end=0.5, text="Both speaking"),
        ])

        result = diariser.diarise(transcript, system_path, mic_path)
        assert result.segments[0].speaker == "Me + Remote"

    def test_out_of_bounds_segment_skipped(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        # 1 second of audio.
        _make_wav(system_path, _silence(duration_s=1.0))
        _make_wav(mic_path, _silence(duration_s=1.0))

        diariser = _make_diariser()
        # Segment starts at 100s, far beyond the 1s file.
        transcript = _make_transcript([
            TranscriptSegment(start=100.0, end=105.0, text="Way past end"),
        ])

        result = diariser.diarise(transcript, system_path, mic_path)
        assert result.segments[0].speaker == ""

    def test_empty_transcript_no_op(self, tmp_path):
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        _make_wav(system_path, _silence())
        _make_wav(mic_path, _silence())

        diariser = _make_diariser()
        transcript = _make_transcript([])

        result = diariser.diarise(transcript, system_path, mic_path)
        assert result.segments == []

    def test_rms_empty_array_returns_zero(self):
        assert Diariser._rms(np.array([])) == 0.0
