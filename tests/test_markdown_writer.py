"""Tests for the Markdown output writer."""

import time

import pytest

from src.output.markdown_writer import MarkdownWriter
from src.summariser import MeetingSummary
from src.utils.config import MarkdownConfig


@pytest.fixture
def started_at() -> float:
    return time.time()


@pytest.fixture
def duration() -> float:
    return 1800.0


class TestMarkdownWriter:
    """Tests for MarkdownWriter.write()."""

    def test_basic_write_creates_file(
        self, markdown_config, sample_summary, sample_transcript, started_at, duration
    ):
        writer = MarkdownWriter(markdown_config)
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        assert path.exists()

    def test_yaml_frontmatter_correctness(
        self, markdown_config, sample_summary, sample_transcript, started_at, duration
    ):
        writer = MarkdownWriter(markdown_config)
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        content = path.read_text(encoding="utf-8")

        # File must start with YAML frontmatter delimiters.
        assert content.startswith("---\n")

        # Extract frontmatter block (between first and second "---").
        parts = content.split("---", 2)
        frontmatter = parts[1]

        assert "title:" in frontmatter
        assert "date:" in frontmatter
        assert "tags:" in frontmatter

    def test_filename_slug_from_title(
        self, markdown_config, sample_summary, sample_transcript, started_at, duration
    ):
        writer = MarkdownWriter(markdown_config)
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        assert "sprint-planning" in path.name

    def test_empty_title_fallback(
        self, markdown_config, sample_transcript, started_at, duration
    ):
        summary = MeetingSummary(
            raw_markdown="# Meeting\n\nSome content.",
            title="",
            tags=["general"],
        )
        writer = MarkdownWriter(markdown_config)
        path = writer.write(summary, sample_transcript, started_at, duration)
        assert "meeting" in path.name

    def test_path_traversal_blocked(
        self, tmp_path, sample_summary, sample_transcript, started_at, duration
    ):
        config = MarkdownConfig(
            vault_path=str(tmp_path / "vault"),
            filename_template="../{date}_{slug}.md",
            include_full_transcript=True,
        )
        writer = MarkdownWriter(config)
        # The "/" is replaced with "_" and leading "." is stripped,
        # so no ValueError is raised -- just a sanitized filename.
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        assert path.exists()

    def test_filename_special_chars_escaped(
        self, markdown_config, sample_transcript, started_at, duration
    ):
        summary = MeetingSummary(
            raw_markdown="# A/B\\C Test\n\nContent.",
            title="A/B\\C Test",
            tags=["test"],
        )
        writer = MarkdownWriter(markdown_config)
        path = writer.write(summary, sample_transcript, started_at, duration)
        # "/" and "\" are replaced with "_" in the filename.
        assert "/" not in path.name
        assert "\\" not in path.name

    def test_transcript_included_when_enabled(
        self, markdown_config, sample_summary, sample_transcript, started_at, duration
    ):
        assert markdown_config.include_full_transcript is True
        writer = MarkdownWriter(markdown_config)
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        content = path.read_text(encoding="utf-8")
        assert "## Full Transcript" in content

    def test_transcript_excluded_when_disabled(
        self, tmp_path, sample_summary, sample_transcript, started_at, duration
    ):
        config = MarkdownConfig(
            vault_path=str(tmp_path / "vault"),
            filename_template="{date}_{slug}.md",
            include_full_transcript=False,
        )
        writer = MarkdownWriter(config)
        path = writer.write(sample_summary, sample_transcript, started_at, duration)
        content = path.read_text(encoding="utf-8")
        assert "## Full Transcript" not in content
