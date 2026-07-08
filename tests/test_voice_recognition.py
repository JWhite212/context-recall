"""Tests for src/voice/recognition.py — clustering, matching, renaming.

All pure logic over numpy arrays with an injected fake embedder;
speechbrain is never imported.
"""

import numpy as np
import pytest

from src.transcriber import Transcript, TranscriptSegment
from src.voice.recognition import (
    VoiceRecogniser,
    cluster_embeddings,
    is_unresolved_label,
    match_profile,
)


def _unit(*values):
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def _transcript(specs):
    """specs: [(speaker, start, end), ...]"""
    segments = [
        TranscriptSegment(start=s, end=e, text=f"segment {i}") for i, (_, s, e) in enumerate(specs)
    ]
    for seg, (speaker, _, _) in zip(segments, specs):
        seg.speaker = speaker
    return Transcript(
        segments=segments,
        language="en",
        language_probability=0.99,
        duration_seconds=max(e for _, _, e in specs),
    )


class FakeEmbedder:
    """Returns canned vectors keyed by window start time."""

    def __init__(self, by_start):
        self.by_start = by_start
        self.calls = []

    def embed_windows(self, audio_path, windows):
        self.calls.append((audio_path, list(windows)))
        return [self.by_start.get(start) for start, _ in windows]


class Config:
    remote_label = "Remote"
    match_threshold = 0.70
    cluster_threshold = 0.60
    min_segment_seconds = 1.0
    split_unmatched_speakers = False


# ----------------------------------------------------------------------
# Label + maths primitives
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Remote", True),
        ("SPEAKER_00", True),
        ("SPEAKER_17", True),
        ("Me", False),
        ("Sarah Chen", False),
        ("Me + Remote", False),
        ("", False),
    ],
)
def test_is_unresolved_label(label, expected):
    assert is_unresolved_label(label, "Remote") is expected


def test_cluster_embeddings_groups_similar_vectors():
    a1, a2 = _unit(1, 0, 0), _unit(0.98, 0.05, 0)
    b1, b2 = _unit(0, 1, 0), _unit(0.05, 0.99, 0)

    clusters = cluster_embeddings([a1, b1, a2, b2], threshold=0.8)

    as_sets = sorted(tuple(sorted(c)) for c in clusters)
    assert as_sets == [(0, 2), (1, 3)]


def test_cluster_embeddings_single_cluster_when_all_similar():
    embs = [_unit(1, 0.01 * i, 0) for i in range(5)]
    clusters = cluster_embeddings(embs, threshold=0.8)
    assert len(clusters) == 1
    assert sorted(clusters[0]) == [0, 1, 2, 3, 4]


def test_match_profile_uses_best_sample_per_person():
    profiles = [
        {"person_id": "p1", "name": "Sarah", "embedding": list(_unit(1, 0, 0))},
        {"person_id": "p1", "name": "Sarah", "embedding": list(_unit(0, 0, 1))},
        {"person_id": "p2", "name": "Marcus", "embedding": list(_unit(0, 1, 0))},
    ]

    person, sim = match_profile(_unit(0.99, 0.02, 0), profiles, threshold=0.7)

    assert person is not None
    assert person["person_id"] == "p1"
    assert sim > 0.95


def test_match_profile_below_threshold_returns_none():
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(_unit(1, 0, 0))}]
    person, _ = match_profile(_unit(0, 1, 0), profiles, threshold=0.7)
    assert person is None


# ----------------------------------------------------------------------
# VoiceRecogniser.identify
# ----------------------------------------------------------------------


def test_identify_renames_matched_remote_speaker(tmp_path):
    voice = _unit(1, 0, 0)
    transcript = _transcript([("Me", 0, 2), ("Remote", 2, 5), ("Remote", 5, 8)])
    embedder = FakeEmbedder({2.0: voice, 5.0: voice})
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(voice)}]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert len(matches) == 1
    assert matches[0].person_id == "p1"
    assert matches[0].new_label == "Sarah"
    assert matches[0].confidence > 0.99
    assert transcript.segments[1].speaker == "Sarah"
    assert transcript.segments[2].speaker == "Sarah"
    assert transcript.segments[0].speaker == "Me"


def test_identify_separates_two_remote_voices_and_matches_each(tmp_path):
    sarah, marcus = _unit(1, 0, 0), _unit(0, 1, 0)
    transcript = _transcript(
        [("Remote", 0, 2), ("Remote", 2, 4), ("Remote", 4, 6), ("Remote", 6, 8)]
    )
    embedder = FakeEmbedder({0.0: sarah, 2.0: marcus, 4.0: sarah, 6.0: marcus})
    profiles = [
        {"person_id": "p1", "name": "Sarah", "embedding": list(sarah)},
        {"person_id": "p2", "name": "Marcus", "embedding": list(marcus)},
    ]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert {m.new_label for m in matches} == {"Sarah", "Marcus"}
    assert [seg.speaker for seg in transcript.segments] == [
        "Sarah",
        "Marcus",
        "Sarah",
        "Marcus",
    ]


def test_identify_skips_short_segments(tmp_path):
    voice = _unit(1, 0, 0)
    transcript = _transcript([("Remote", 0, 0.5)])  # below min_segment_seconds
    embedder = FakeEmbedder({0.0: voice})
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(voice)}]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert matches == []
    assert embedder.calls == []
    assert transcript.segments[0].speaker == "Remote"


def test_identify_leaves_unmatched_cluster_alone_by_default(tmp_path):
    unknown = _unit(0, 0, 1)
    transcript = _transcript([("Remote", 0, 2)])
    embedder = FakeEmbedder({0.0: unknown})
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(_unit(1, 0, 0))}]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert matches == []
    assert transcript.segments[0].speaker == "Remote"


def test_identify_splits_unmatched_clusters_when_configured(tmp_path):
    v1, v2 = _unit(1, 0, 0), _unit(0, 1, 0)
    transcript = _transcript(
        [("Remote", 0, 2), ("Remote", 2, 4), ("Remote", 4, 6), ("Remote", 6, 8)]
    )
    embedder = FakeEmbedder({0.0: v1, 2.0: v1, 4.0: v1, 6.0: v2})

    config = Config()
    config.split_unmatched_speakers = True
    matches = VoiceRecogniser(embedder, config).identify(transcript, tmp_path / "a.wav", [])

    # Largest cluster keeps "Remote"; the second voice becomes "Remote 2".
    assert [m.new_label for m in matches] == ["Remote 2"]
    assert [seg.speaker for seg in transcript.segments] == [
        "Remote",
        "Remote",
        "Remote",
        "Remote 2",
    ]


def test_identify_handles_pyannote_labels(tmp_path):
    voice = _unit(1, 0, 0)
    transcript = _transcript([("SPEAKER_00", 0, 2), ("SPEAKER_01", 2, 4)])
    embedder = FakeEmbedder({0.0: voice, 2.0: _unit(0, 1, 0)})
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(voice)}]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert len(matches) == 1
    assert matches[0].original_label == "SPEAKER_00"
    assert transcript.segments[0].speaker == "Sarah"
    assert transcript.segments[1].speaker == "SPEAKER_01"


def test_identify_ignores_segments_with_failed_embeddings(tmp_path):
    transcript = _transcript([("Remote", 0, 2), ("Remote", 2, 4)])
    embedder = FakeEmbedder({0.0: None, 2.0: None})  # silent windows
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": list(_unit(1, 0, 0))}]

    matches = VoiceRecogniser(embedder, Config()).identify(transcript, tmp_path / "a.wav", profiles)

    assert matches == []
    assert transcript.segments[0].speaker == "Remote"
