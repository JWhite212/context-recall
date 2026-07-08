"""Tests for src/voice/enrolment.py — window extraction + sample building."""

import json

import numpy as np
import pytest

from src.voice.enrolment import build_enrolment_sample, extract_speaker_windows


def _transcript_json(segments):
    return json.dumps({"segments": segments})


class FakeEmbedder:
    def __init__(self, vectors):
        self._vectors = vectors

    def embed_windows(self, audio_path, windows):
        return self._vectors[: len(windows)]


def test_extract_speaker_windows_filters_by_speaker_and_duration():
    tj = _transcript_json(
        [
            {"speaker": "Remote", "start": 0.0, "end": 3.0},
            {"speaker": "Me", "start": 3.0, "end": 6.0},
            {"speaker": "Remote", "start": 6.0, "end": 6.4},  # too short
            {"speaker": "Remote", "start": 7.0, "end": 10.0},
        ]
    )

    windows = extract_speaker_windows(tj, "Remote", min_seconds=1.0)

    assert windows == [(0.0, 3.0), (7.0, 10.0)]


def test_extract_speaker_windows_tolerates_bad_json():
    assert extract_speaker_windows("{not json", "Remote", 1.0) == []
    assert extract_speaker_windows(None, "Remote", 1.0) == []
    assert extract_speaker_windows("", "Remote", 1.0) == []


def test_extract_speaker_windows_caps_window_count():
    segments = [{"speaker": "Remote", "start": float(i), "end": float(i) + 2.0} for i in range(100)]
    windows = extract_speaker_windows(_transcript_json(segments), "Remote", 1.0)
    assert len(windows) == 40


def test_build_enrolment_sample_averages_and_normalises(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 64)
    v1 = np.array([1.0, 0.0], dtype=np.float32)
    v2 = np.array([0.0, 1.0], dtype=np.float32)

    sample = build_enrolment_sample(FakeEmbedder([v1, v2]), audio, [(0, 2), (2, 4)])

    assert sample is not None
    assert sample["segment_count"] == 2
    assert sample["duration_seconds"] == 4.0
    emb = np.array(sample["embedding"])
    assert np.linalg.norm(emb) == pytest.approx(1.0)
    assert emb[0] == pytest.approx(emb[1])


def test_build_enrolment_sample_skips_failed_windows(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 64)
    v = np.array([1.0, 0.0], dtype=np.float32)

    sample = build_enrolment_sample(FakeEmbedder([None, v]), audio, [(0, 2), (2, 4)])

    assert sample["segment_count"] == 1
    assert sample["duration_seconds"] == 2.0


def test_build_enrolment_sample_none_when_nothing_usable(tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\x00" * 64)
    assert build_enrolment_sample(FakeEmbedder([None]), audio, [(0, 2)]) is None
    assert build_enrolment_sample(FakeEmbedder([]), audio, []) is None


def test_build_enrolment_sample_none_when_audio_missing(tmp_path):
    v = np.array([1.0, 0.0], dtype=np.float32)
    missing = tmp_path / "gone.wav"
    assert build_enrolment_sample(FakeEmbedder([v]), missing, [(0, 2)]) is None
