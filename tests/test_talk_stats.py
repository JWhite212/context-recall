"""Tests for src/talk_stats.py — per-speaker talk-time computation."""

import json

from src.talk_stats import compute_talk_stats


def _tj(segments):
    return json.dumps({"segments": segments})


def test_empty_and_malformed_inputs():
    assert compute_talk_stats(None) == {"speakers": [], "total_speaking_seconds": 0.0}
    assert compute_talk_stats("") == {"speakers": [], "total_speaking_seconds": 0.0}
    assert compute_talk_stats("{bad json") == {
        "speakers": [],
        "total_speaking_seconds": 0.0,
    }
    assert compute_talk_stats(_tj([])) == {"speakers": [], "total_speaking_seconds": 0.0}


def test_percentages_and_ordering():
    stats = compute_talk_stats(
        _tj(
            [
                {"start": 0, "end": 30, "speaker": "Me", "text": "a"},
                {"start": 30, "end": 40, "speaker": "Sarah", "text": "b"},
                {"start": 40, "end": 70, "speaker": "Me", "text": "c"},
            ]
        )
    )
    assert stats["total_speaking_seconds"] == 70.0
    assert [s["speaker"] for s in stats["speakers"]] == ["Me", "Sarah"]
    me = stats["speakers"][0]
    assert me["seconds"] == 60.0
    assert me["percent"] == 85.7
    assert stats["speakers"][1]["percent"] == 14.3


def test_turns_and_longest_monologue():
    stats = compute_talk_stats(
        _tj(
            [
                {"start": 0, "end": 10, "speaker": "Me", "text": "a"},
                {"start": 10, "end": 25, "speaker": "Me", "text": "b"},  # same turn
                {"start": 25, "end": 30, "speaker": "Sarah", "text": "c"},
                {"start": 30, "end": 35, "speaker": "Me", "text": "d"},
            ]
        )
    )
    me = next(s for s in stats["speakers"] if s["speaker"] == "Me")
    sarah = next(s for s in stats["speakers"] if s["speaker"] == "Sarah")
    assert me["turns"] == 2
    assert me["longest_monologue_seconds"] == 25.0
    assert sarah["turns"] == 1


def test_unlabelled_segments_grouped():
    stats = compute_talk_stats(
        _tj(
            [
                {"start": 0, "end": 5, "speaker": "", "text": "a"},
                {"start": 5, "end": 9, "text": "b"},
            ]
        )
    )
    assert stats["speakers"][0]["speaker"] == "Unlabelled"
    assert stats["speakers"][0]["seconds"] == 9.0
