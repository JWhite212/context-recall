"""Tests for the Notion output writer."""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.output.notion_writer import NotionWriter
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import NotionConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_writer(api_key: str = "", database_id: str = "") -> NotionWriter:
    """Create a NotionWriter with the given config overrides."""
    config = NotionConfig(
        enabled=True,
        api_key=api_key,
        database_id=database_id,
    )
    return NotionWriter(config)


# ---------------------------------------------------------------------------
# Block conversion tests
# ---------------------------------------------------------------------------


class TestNotionMarkdownConversion:
    """Tests for _markdown_to_notion_blocks()."""

    def _writer(self) -> NotionWriter:
        return _make_writer()

    def test_heading_levels_h1_h2_h3(self):
        md = "# Heading One\n## Heading Two\n### Heading Three"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 3
        assert blocks[0]["type"] == "heading_1"
        assert blocks[1]["type"] == "heading_2"
        assert blocks[2]["type"] == "heading_3"

    def test_bullet_items(self):
        md = "- First item\n- Second item"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "bulleted_list_item"
        assert blocks[1]["type"] == "bulleted_list_item"

    def test_checkbox_items_unchecked(self):
        md = "- [ ] Buy groceries"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is False

    def test_checkbox_items_checked(self):
        md = "- [x] Done task\n- [X] Also done"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 2
        for block in blocks:
            assert block["type"] == "to_do"
            assert block["to_do"]["checked"] is True

    def test_divider(self):
        md = "---"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "divider"
        assert blocks[0]["divider"] == {}

    def test_paragraph_fallback(self):
        md = "Just some plain text."
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    def test_empty_lines_skipped(self):
        md = "Line one\n\n\nLine two\n\n"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 2
        assert all(b["type"] == "paragraph" for b in blocks)


# ---------------------------------------------------------------------------
# Rich text splitting tests
# ---------------------------------------------------------------------------


class TestNotionRichText:
    """Tests for _rich_text() text chunking."""

    def test_short_text_single_chunk(self):
        text = "Hello, world!"
        result = NotionWriter._rich_text(text)
        assert len(result) == 1
        assert result[0]["text"]["content"] == text

    def test_long_text_chunked(self):
        text = "A" * 4500
        result = NotionWriter._rich_text(text)
        assert len(result) == 3
        assert len(result[0]["text"]["content"]) == 2000
        assert len(result[1]["text"]["content"]) == 2000
        assert len(result[2]["text"]["content"]) == 500


# ---------------------------------------------------------------------------
# Writer integration tests
# ---------------------------------------------------------------------------


class TestNotionWriter:
    """Tests for NotionWriter.write() and error handling."""

    @pytest.fixture
    def summary(self) -> MeetingSummary:
        return MeetingSummary(
            raw_markdown="## Summary\nDiscussed roadmap.\n- Action item one",
            title="Sprint Planning",
            tags=["planning", "roadmap"],
        )

    @pytest.fixture
    def transcript(self) -> Transcript:
        return Transcript(
            segments=[
                TranscriptSegment(start=0.0, end=5.0, text="Hello."),
            ],
            language="en",
            language_probability=0.98,
            duration_seconds=5.0,
        )

    @pytest.fixture
    def started_at(self) -> float:
        return time.time()

    @pytest.fixture
    def duration(self) -> float:
        return 1800.0

    def test_missing_database_id_raises(
        self, summary, transcript, started_at, duration
    ):
        writer = _make_writer(api_key="some-key", database_id="")
        with pytest.raises(ValueError, match="database ID"):
            writer.write(summary, transcript, started_at, duration)

    def test_missing_api_key_raises(
        self, summary, transcript, started_at, duration
    ):
        writer = _make_writer(api_key="", database_id="some-db-id")
        with pytest.raises(ValueError, match="API key"):
            writer.write(summary, transcript, started_at, duration)

    @patch("src.output.notion_writer.NotionClient")
    def test_write_creates_page(
        self, mock_client_cls, summary, transcript, started_at, duration
    ):
        mock_client = MagicMock()
        mock_client.pages.create.return_value = {
            "url": "https://notion.so/page-123",
            "id": "page-123",
        }
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="test-key", database_id="test-db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url == "https://notion.so/page-123"
        mock_client.pages.create.assert_called_once()

        call_kwargs = mock_client.pages.create.call_args
        assert call_kwargs.kwargs["parent"] == {"database_id": "test-db"}
        assert "properties" in call_kwargs.kwargs
        assert "children" in call_kwargs.kwargs

    @patch("src.output.notion_writer.NotionClient")
    def test_block_batching_over_100(
        self, mock_client_cls, transcript, started_at, duration
    ):
        # Build a summary whose markdown produces more than 100 blocks.
        lines = [f"- Item {i}" for i in range(150)]
        big_md = "\n".join(lines)
        big_summary = MeetingSummary(
            raw_markdown=big_md,
            title="Big Meeting",
            tags=["test"],
        )

        mock_client = MagicMock()
        mock_client.pages.create.return_value = {
            "url": "https://notion.so/big-page",
            "id": "big-page-id",
        }
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="test-key", database_id="test-db")
        writer.write(big_summary, transcript, started_at, duration)

        # The first 100 blocks go in pages.create; the rest via blocks.children.append.
        mock_client.pages.create.assert_called_once()
        assert mock_client.blocks.children.append.call_count >= 1

        # Verify the append was called with the correct page ID.
        append_call = mock_client.blocks.children.append.call_args
        assert append_call.kwargs["block_id"] == "big-page-id"
