from src.output.note_assembler import render_transcript
from src.transcriber import Transcript, TranscriptSegment


def _t():
    return Transcript(
        segments=[TranscriptSegment(start=0.0, end=2.0, text="Hello", speaker="Me")],
        language="en",
        duration_seconds=2.0,
    )


def test_inline_lists_segments():
    out = render_transcript(_t(), "inline")
    assert "## Full Transcript" in out
    assert "**[00:00:00]** *Me*: Hello" in out


def test_foldout_wraps_in_quote_callout():
    out = render_transcript(_t(), "foldout")
    assert out.startswith("> [!quote]- Full transcript")
    assert "> **[00:00:00]** *Me*: Hello" in out


def test_omit_returns_empty():
    assert render_transcript(_t(), "omit") == ""


def test_linked_returns_empty_here():
    # The companion note + link is the writer's job (it knows the vault path).
    assert render_transcript(_t(), "linked") == ""


def test_none_transcript_returns_empty():
    assert render_transcript(None, "inline") == ""


def test_segment_without_speaker_has_no_label():
    t = Transcript(
        segments=[TranscriptSegment(start=0.0, end=1.0, text="Hi", speaker="")],
        language="en",
        duration_seconds=1.0,
    )
    assert "**[00:00:00]** Hi" in render_transcript(t, "inline")
