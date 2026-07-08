"""Tests for SpeakerSuggester — transcript-evidence name suggestions."""

import json
from unittest.mock import patch

from src.people.suggester import SpeakerSuggester
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import SummarisationConfig


def _transcript(specs):
    segments = []
    for i, (speaker, text) in enumerate(specs):
        seg = TranscriptSegment(start=float(i), end=float(i + 1), text=text)
        seg.speaker = speaker
        segments.append(seg)
    return Transcript(
        segments=segments,
        language="en",
        language_probability=0.99,
        duration_seconds=float(len(specs)),
    )


def _suggester():
    return SpeakerSuggester(SummarisationConfig())


def test_suggest_returns_parsed_suggestions_for_unresolved_labels():
    transcript = _transcript([("Me", "Hey there"), ("Remote", "Hi, it's Sarah here from Acme")])
    response = json.dumps(
        [
            {
                "speaker_label": "Remote",
                "suggested_name": "Sarah",
                "evidence": "Hi, it's Sarah here",
            }
        ]
    )

    suggester = _suggester()
    with patch.object(suggester, "_call_llm", return_value=response) as call:
        suggestions = suggester.suggest(transcript)

    call.assert_called_once()
    assert suggestions == [
        {
            "speaker_label": "Remote",
            "suggested_name": "Sarah",
            "evidence": "Hi, it's Sarah here",
        }
    ]


def test_suggest_skips_llm_when_no_unresolved_labels():
    transcript = _transcript([("Me", "hello"), ("Sarah Chen", "hi")])
    suggester = _suggester()
    with patch.object(suggester, "_call_llm") as call:
        assert suggester.suggest(transcript) == []
    call.assert_not_called()


def test_suggest_drops_labels_that_are_not_unresolved():
    transcript = _transcript([("Remote", "Hi, it's Sarah")])
    response = json.dumps(
        [
            {"speaker_label": "Remote", "suggested_name": "Sarah"},
            {"speaker_label": "Me", "suggested_name": "Jamie"},  # resolved label
            {"speaker_label": "SPEAKER_09", "suggested_name": "Ghost"},  # not present
        ]
    )
    suggester = _suggester()
    with patch.object(suggester, "_call_llm", return_value=response):
        suggestions = suggester.suggest(transcript)
    assert [s["suggested_name"] for s in suggestions] == ["Sarah"]


def test_suggest_handles_fenced_and_garbage_responses():
    transcript = _transcript([("Remote", "Hi, it's Sarah")])
    suggester = _suggester()

    fenced = '```json\n[{"speaker_label": "Remote", "suggested_name": "Sarah"}]\n```'
    with patch.object(suggester, "_call_llm", return_value=fenced):
        assert suggester.suggest(transcript)[0]["suggested_name"] == "Sarah"

    with patch.object(suggester, "_call_llm", return_value="I could not find any"):
        assert suggester.suggest(transcript) == []

    with patch.object(suggester, "_call_llm", return_value=""):
        assert suggester.suggest(transcript) == []


def test_suggest_survives_llm_failure():
    transcript = _transcript([("Remote", "Hi, it's Sarah")])
    suggester = _suggester()
    with patch.object(suggester, "_call_llm", side_effect=RuntimeError("ollama down")):
        assert suggester.suggest(transcript) == []


def test_pyannote_labels_count_as_unresolved():
    transcript = _transcript([("SPEAKER_00", "This is Marcus speaking")])
    response = json.dumps([{"speaker_label": "SPEAKER_00", "suggested_name": "Marcus"}])
    suggester = _suggester()
    with patch.object(suggester, "_call_llm", return_value=response):
        suggestions = suggester.suggest(transcript)
    assert suggestions[0]["speaker_label"] == "SPEAKER_00"


def test_long_transcript_truncation_preserves_line_structure():
    """Truncation must keep whole [Speaker] lines, not flatten to words."""
    long_line = "word " * 200
    specs = [("Remote" if i % 2 else "Me", long_line.strip()) for i in range(60)]
    transcript = _transcript(specs)

    suggester = _suggester()
    captured = {}

    def capture(text):
        captured["text"] = text
        return "[]"

    with patch.object(suggester, "_call_llm", side_effect=capture):
        suggester.suggest(transcript)

    text = captured["text"]
    assert "..." in text, "long transcript must be truncated"
    lines = [line for line in text.splitlines() if line and line != "..."]
    assert all("[" in line and "]" in line for line in lines), (
        "every kept line must retain its [speaker] prefix"
    )
    assert len(text.split()) < 60 * 200
