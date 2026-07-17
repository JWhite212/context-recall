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


class _FakeRepo:
    def __init__(self, meeting):
        self._meeting = meeting
        self.updated: dict = {}

    async def get_meeting(self, mid):
        return self._meeting

    async def update_meeting(self, mid, **fields):
        self.updated.update(fields)
        for k, v in fields.items():
            setattr(self._meeting, k, v)


class _FakeDb:
    def __init__(self, repo):
        self.repo = repo
        self.database = object()  # non-None: re-render proceeds


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


def test_build_note_context_and_write_note_idempotent_by_title(tmp_path):
    writer = MarkdownWriter(_cfg(tmp_path))
    seg = TranscriptSegment(start=0.0, end=2.0, text="hi", speaker="Me")
    transcript = Transcript(segments=[seg], language="en", duration_seconds=2.0)
    meeting = _FakeMeeting()
    runner = _runner(_cfg(tmp_path), writer)

    ctx = runner._build_note_context(meeting, transcript, enriched=False)
    first = writer.write_note(ctx)
    assert first is not None
    ctx2 = runner._build_note_context(meeting, transcript, enriched=True)
    second = writer.write_note(ctx2)
    assert second == first
    assert len(list(Path(tmp_path).glob("*.md"))) == 1


@pytest.mark.asyncio
async def test_rerender_updates_same_file_via_markdown_path(tmp_path):
    writer = MarkdownWriter(_cfg(tmp_path))
    meeting = _FakeMeeting()
    repo = _FakeRepo(meeting)
    runner = _runner(_cfg(tmp_path), writer, db=_FakeDb(repo))

    # Pass 1: write the pre-enrichment note and record its path.
    seg = TranscriptSegment(start=0.0, end=2.0, text="hi", speaker="Me")
    transcript = Transcript(segments=[seg], language="en", duration_seconds=2.0)
    first = writer.write_note(runner._build_note_context(meeting, transcript, enriched=False))
    meeting.markdown_path = str(first)

    # Pass 2: re-render must rewrite the SAME file, not duplicate it.
    await runner._rerender_markdown_async("m1")
    assert first.exists()
    assert len(list(Path(tmp_path).glob("*.md"))) == 1
    # markdown_path unchanged (same file) so no redundant DB write of it.
    assert "markdown_path" not in repo.updated

    # A moved title on re-render still lands on the original file (reuse_path).
    meeting.title = "Completely Different Title"
    await runner._rerender_markdown_async("m1")
    assert len(list(Path(tmp_path).glob("*.md"))) == 1
    assert Path(meeting.markdown_path) == first


def test_transcript_from_dict_matches_stored_shape():
    # Guards the from_dict call inside _rerender_markdown_async.
    data = json.loads(_FakeMeeting.transcript_json)
    t = Transcript.from_dict(data)
    assert t.segments[0].speaker == "Me" and t.segments[0].text == "hi"
