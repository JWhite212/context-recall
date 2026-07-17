import json
from pathlib import Path

import pytest

from src.output.markdown_writer import MarkdownWriter
from src.pipeline_runner import PipelineRunner
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


class _FakeMeeting:
    id = "m1"
    title = "Daily Standup"
    started_at = 1_752_570_180.0
    duration_seconds = 1680.0
    transcript_json = '{"segments": [{"start": 0, "end": 2, "text": "hi", "speaker": "Me"}]}'
    summary_markdown = "# Daily Standup\n\n## Summary\n\nWe met.\n"
    tags = ["standup"]
    word_count = 4456
    attendees_json = "[]"
    series_id = None
    client_id = None
    project_id = None
    assignment_source = ""
    template_name = "standup"
    markdown_path = ""


class _Db:
    """Minimal DbBridge stand-in backed by a real Database + repo."""

    def __init__(self, database, repo):
        self.database = database
        self.repo = repo


def _runner(cfg, writer, db=None):
    runner = PipelineRunner.__new__(PipelineRunner)

    class _Cfg:
        markdown = cfg

    runner._config = _Cfg()
    runner._md_writer = writer
    runner._emit_cb = None
    runner._db = db
    return runner


def _cfg(tmp_path):
    return MarkdownConfig(
        enabled=True,
        vault_path=str(tmp_path),
        filename_template="{date}_{slug}.md",
        include_full_transcript=False,
    )


def test_build_note_context_reuse_path_relocates_to_single_note(tmp_path):
    writer = MarkdownWriter(_cfg(tmp_path))
    seg = TranscriptSegment(start=0.0, end=2.0, text="hi", speaker="Me")
    transcript = Transcript(segments=[seg], language="en", duration_seconds=2.0)
    meeting = _FakeMeeting()
    runner = _runner(_cfg(tmp_path), writer)

    # Pass 1 (pre-enrichment) writes flat at the vault root.
    first = writer.write_note(runner._build_note_context(meeting, transcript, enriched=False))
    assert first is not None and first.parent == Path(tmp_path)

    # Pass 2 (enriched re-render) reuses the path; the unknown client routes it
    # to Unsorted/, moving the note rather than duplicating it.
    writer.reuse_path(first)
    second = writer.write_note(runner._build_note_context(meeting, transcript, enriched=True))
    assert second.parent.name == "Unsorted"
    assert not first.exists()
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


@pytest.mark.asyncio
async def test_rerender_updates_same_file_via_markdown_path(tmp_path, db, repo):
    writer = MarkdownWriter(_cfg(tmp_path))
    runner = _runner(_cfg(tmp_path), writer, db=_Db(db, repo))

    mid = await repo.create_meeting(started_at=1_752_570_180.0)
    await repo.update_meeting(
        mid,
        title="Daily Standup",
        duration_seconds=1680.0,
        transcript_json='{"segments": [{"start": 0, "end": 2, "text": "hi", "speaker": "Me"}]}',
        summary_markdown="# Daily Standup\n\n## Summary\n\nWe met.\n",
        tags=["standup"],
        word_count=4456,
        status="complete",
    )

    # Pass 1: write the pre-enrichment note (flat) and record its path.
    meeting = await repo.get_meeting(mid)
    transcript = Transcript.from_dict(json.loads(meeting.transcript_json))
    first = writer.write_note(runner._build_note_context(meeting, transcript, enriched=False))
    await repo.update_meeting(mid, markdown_path=str(first))

    # Pass 2: re-render enriches in place; the unknown client routes it to
    # Unsorted/, so there is still exactly ONE note and it carries enriched: true.
    await runner._rerender_markdown_async(mid)
    after = await repo.get_meeting(mid)
    current = Path(after.markdown_path)
    assert current.exists()
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1
    assert "enriched: true" in current.read_text(encoding="utf-8")
    basename = current.name

    # A moved title on re-render keeps the SAME file (basename preserved):
    # title -> filename changes are the rename path's job, not the re-render's.
    await repo.update_meeting(mid, title="Completely Different Title")
    await runner._rerender_markdown_async(mid)
    after2 = await repo.get_meeting(mid)
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1
    assert Path(after2.markdown_path).name == basename


def test_transcript_from_dict_matches_stored_shape():
    # Guards the from_dict call inside _rerender_markdown_async.
    data = json.loads(_FakeMeeting.transcript_json)
    t = Transcript.from_dict(data)
    assert t.segments[0].speaker == "Me" and t.segments[0].text == "hi"
