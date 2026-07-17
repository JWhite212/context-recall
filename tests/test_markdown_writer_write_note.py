from pathlib import Path

import yaml

from src.output.markdown_writer import MarkdownWriter
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _cfg(tmp_path: Path) -> MarkdownConfig:
    return MarkdownConfig(
        enabled=True,
        vault_path=str(tmp_path),
        filename_template="{date}_{slug}.md",
        include_full_transcript=False,
    )


def _transcript() -> Transcript:
    seg = TranscriptSegment(start=0.0, end=2.0, text="Hello there", speaker="Me")
    return Transcript(segments=[seg], language="en", duration_seconds=2.0)


def test_write_via_summary_adapter_still_writes_note(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    summary = MeetingSummary(
        raw_markdown="# Daily Standup\n\n## Summary\n\nWe met.\n",
        title="Daily Standup",
        tags=["standup", "team"],
    )
    path = w.write(summary, _transcript(), started_at=1_752_570_180.0, duration_seconds=1680.0)
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm = yaml.safe_load(text.split("---\n")[1])
    assert fm["title"] == "Daily Standup"
    assert fm["tags"] == ["standup", "team"]
    assert "We met." in text
    assert "—" not in text  # no em dash in the footer


def test_write_note_returns_none_and_sets_error_on_unwritable_vault(tmp_path):
    (tmp_path / "file_not_dir").write_text("x")  # a file where a dir is expected
    cfg = _cfg(tmp_path / "file_not_dir")
    w = MarkdownWriter(cfg)
    summary = MeetingSummary(raw_markdown="# T\n", title="T", tags=[])
    path = w.write(summary, _transcript(), started_at=1_752_570_180.0, duration_seconds=60.0)
    assert path is None and w.last_error
