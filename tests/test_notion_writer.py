"""Tests for the Notion output writer."""

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from notion_client.errors import APIResponseError, HTTPResponseError

from src.output.notion_writer import NotionWriter
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import NotionConfig


def _http_error(status: int, *, retry_after: str | None = None) -> HTTPResponseError:
    """Build a notion_client HTTPResponseError with the given status."""
    headers_dict = {"Retry-After": retry_after} if retry_after else {}
    return HTTPResponseError(
        code="server_error",
        status=status,
        message=f"HTTP {status}",
        headers=httpx.Headers(headers_dict),
        raw_body_text="",
    )


def _api_error(status: int = 400) -> APIResponseError:
    """Build a notion_client APIResponseError (4xx) for tests."""
    return APIResponseError(
        code="validation_error",
        status=status,
        message=f"HTTP {status}",
        headers=httpx.Headers({}),
        raw_body_text="",
    )


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

    def test_h4_heading_becomes_paragraph(self):
        md = "#### H4 Heading"
        blocks = self._writer()._markdown_to_notion_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"
        text = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
        assert text == "#### H4 Heading"


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

    def test_rich_text_empty_string(self):
        result = NotionWriter._rich_text("")
        assert len(result) == 1
        assert result[0] == {"type": "text", "text": {"content": ""}}

    def test_rich_text_exact_boundary(self):
        result = NotionWriter._rich_text("a" * 2000)
        assert len(result) == 1
        assert result[0]["text"]["content"] == "a" * 2000


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

    def test_missing_database_id_raises(self, summary, transcript, started_at, duration):
        writer = _make_writer(api_key="some-key", database_id="")
        with pytest.raises(ValueError, match="database ID"):
            writer.write(summary, transcript, started_at, duration)

    def test_missing_api_key_raises(self, summary, transcript, started_at, duration):
        writer = _make_writer(api_key="", database_id="some-db-id")
        with pytest.raises(ValueError, match="API key"):
            writer.write(summary, transcript, started_at, duration)

    @patch("src.output.notion_writer.NotionClient")
    def test_write_creates_page(self, mock_client_cls, summary, transcript, started_at, duration):
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
    def test_block_batching_over_100(self, mock_client_cls, transcript, started_at, duration):
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

    @patch("src.output.notion_writer.NotionClient")
    def test_write_no_tags(self, mock_client_cls, transcript, started_at, duration):
        summary = MeetingSummary(
            raw_markdown="## Summary\nNo tags here.",
            title="Tagless Meeting",
            tags=[],
        )

        mock_client = MagicMock()
        mock_client.pages.create.return_value = {
            "url": "https://notion.so/no-tags",
            "id": "no-tags-id",
        }
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="test-key", database_id="test-db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url == "https://notion.so/no-tags"
        call_kwargs = mock_client.pages.create.call_args.kwargs
        properties = call_kwargs["properties"]

        # Empty tags list should not produce a Tags multi_select property.
        assert "Tags" not in properties


# ---------------------------------------------------------------------------
# Retry / error handling tests
# ---------------------------------------------------------------------------


class TestNotionRetryAndErrorHandling:
    """Tests for retry-on-5xx, Retry-After honour, and last_error stashing."""

    @pytest.fixture
    def summary(self) -> MeetingSummary:
        return MeetingSummary(
            raw_markdown="## Summary\nShort.",
            title="Retry Test",
            tags=["t"],
        )

    @pytest.fixture
    def transcript(self) -> Transcript:
        return Transcript(
            segments=[TranscriptSegment(start=0.0, end=1.0, text="Hi.")],
            language="en",
            language_probability=0.99,
            duration_seconds=1.0,
        )

    @pytest.fixture
    def started_at(self) -> float:
        return time.time()

    @pytest.fixture
    def duration(self) -> float:
        return 60.0

    @patch("src.output.notion_writer.time.sleep", return_value=None)
    @patch("src.output.notion_writer.NotionClient")
    def test_retries_on_5xx_then_succeeds(
        self,
        mock_client_cls,
        _mock_sleep,
        summary,
        transcript,
        started_at,
        duration,
    ):
        """Transient 5xx errors retry and a later success is returned."""
        mock_client = MagicMock()
        mock_client.pages.create.side_effect = [
            _http_error(500),
            _http_error(503),
            {"url": "https://notion.so/ok", "id": "ok-id"},
        ]
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url == "https://notion.so/ok"
        assert mock_client.pages.create.call_count == 3
        assert writer.last_error is None

    @patch("src.output.notion_writer.time.sleep", return_value=None)
    @patch("src.output.notion_writer.NotionClient")
    def test_5xx_exhausted_returns_none_and_sets_last_error(
        self,
        mock_client_cls,
        _mock_sleep,
        summary,
        transcript,
        started_at,
        duration,
    ):
        """Exhausting retries on 5xx returns None and stashes last_error."""
        mock_client = MagicMock()
        mock_client.pages.create.side_effect = _http_error(502)
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url is None
        assert mock_client.pages.create.call_count == 3
        assert writer.last_error is not None
        assert "502" in writer.last_error

    @patch("src.output.notion_writer.time.sleep", return_value=None)
    @patch("src.output.notion_writer.NotionClient")
    def test_4xx_does_not_retry_and_stashes_last_error(
        self,
        mock_client_cls,
        _mock_sleep,
        summary,
        transcript,
        started_at,
        duration,
    ):
        """4xx APIResponseError bypasses retry and is stashed on last_error."""
        mock_client = MagicMock()
        mock_client.pages.create.side_effect = _api_error(400)
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url is None
        assert mock_client.pages.create.call_count == 1
        assert writer.last_error is not None
        assert "400" in writer.last_error

    @patch("src.output.notion_writer.time.sleep", return_value=None)
    @patch("src.output.notion_writer.NotionClient")
    def test_429_honours_retry_after_header(
        self,
        mock_client_cls,
        mock_sleep,
        summary,
        transcript,
        started_at,
        duration,
    ):
        """A 429 with Retry-After=N causes sleep(N) before the next attempt."""
        mock_client = MagicMock()
        mock_client.pages.create.side_effect = [
            _http_error(429, retry_after="7"),
            {"url": "https://notion.so/after-429", "id": "id-429"},
        ]
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        url = writer.write(summary, transcript, started_at, duration)

        assert url == "https://notion.so/after-429"
        mock_sleep.assert_any_call(7.0)
        assert writer.last_error is None

    @patch("src.output.notion_writer.time.sleep", return_value=None)
    @patch("src.output.notion_writer.NotionClient")
    def test_last_error_cleared_on_subsequent_success(
        self,
        mock_client_cls,
        _mock_sleep,
        summary,
        transcript,
        started_at,
        duration,
    ):
        """A write() that succeeds clears last_error from a prior failure."""
        mock_client = MagicMock()
        mock_client.pages.create.return_value = {
            "url": "https://notion.so/p",
            "id": "p",
        }
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        writer.last_error = "old failure"
        url = writer.write(summary, transcript, started_at, duration)

        assert url == "https://notion.so/p"
        assert writer.last_error is None


class TestNotionPageIdentity:
    """last_page_id + archive_page: the reprocess update-or-create story."""

    def _summary(self) -> MeetingSummary:
        return MeetingSummary(raw_markdown="## S\nBody.", title="T", tags=[])

    def _transcript(self) -> Transcript:
        return Transcript(
            segments=[TranscriptSegment(start=0.0, end=1.0, text="Hi.")],
            language="en",
            language_probability=0.9,
            duration_seconds=1.0,
        )

    @patch("src.output.notion_writer.NotionClient")
    def test_write_records_last_page_id(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.pages.create.return_value = {
            "url": "https://notion.so/page-123",
            "id": "page-123",
        }
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        writer.write(self._summary(), self._transcript(), time.time(), 60.0)

        assert writer.last_page_id == "page-123"

    @patch("src.output.notion_writer.NotionClient")
    def test_failed_write_leaves_last_page_id_none(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.pages.create.side_effect = _api_error(400)
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        writer.last_page_id = "stale"
        writer.write(self._summary(), self._transcript(), time.time(), 60.0)

        assert writer.last_page_id is None

    @patch("src.output.notion_writer.NotionClient")
    def test_archive_page_marks_page_archived(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        assert writer.archive_page("old-page") is True

        mock_client.pages.update.assert_called_once_with(page_id="old-page", archived=True)

    @patch("src.output.notion_writer.NotionClient")
    def test_archive_page_failure_is_swallowed(self, mock_client_cls):
        """Archiving a page the user already deleted must not raise."""
        mock_client = MagicMock()
        mock_client.pages.update.side_effect = RuntimeError("page gone")
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        assert writer.archive_page("gone-page") is False

    def test_archive_page_empty_id_is_noop(self):
        writer = _make_writer(api_key="k", database_id="db")
        assert writer.archive_page("") is False


class TestNotionUpdatePageTitle:
    """update_page_title(): best-effort PATCH of the title property."""

    @patch("src.output.notion_writer.NotionClient")
    def test_update_page_title_patches_title_property(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        ok = writer.update_page_title("page-123", "Renamed Meeting")

        assert ok is True
        title_prop = writer._config.properties["title"]
        mock_client.pages.update.assert_called_once_with(
            page_id="page-123",
            properties={title_prop: {"title": NotionWriter._rich_text("Renamed Meeting")}},
        )

    @patch("src.output.notion_writer.NotionClient")
    def test_update_page_title_failure_is_swallowed(self, mock_client_cls):
        """A page the user already deleted must not raise."""
        mock_client = MagicMock()
        mock_client.pages.update.side_effect = RuntimeError("page gone")
        mock_client_cls.return_value = mock_client

        writer = _make_writer(api_key="k", database_id="db")
        assert writer.update_page_title("gone-page", "New Title") is False

    def test_update_page_title_empty_id_is_noop(self):
        writer = _make_writer(api_key="k", database_id="db")
        assert writer.update_page_title("", "New Title") is False
