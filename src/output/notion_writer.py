"""
Notion output writer.

Creates a new page in a configured Notion database for each meeting.
The page includes the summary as page content and metadata as
database properties (title, date, tags, status).

Requires a Notion internal integration with access to the target
database. See: https://developers.notion.com/docs/create-a-notion-integration

Content is built using Notion's block API, which means headings,
bullet lists, and to-do items from the summary are preserved as
native Notion blocks rather than dumped as plain text.
"""

import logging
import time
from typing import Any, Callable

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError, HTTPResponseError

from src.summariser import MeetingSummary
from src.transcriber import Transcript
from src.utils.config import NotionConfig

logger = logging.getLogger(__name__)

# Retry tuning for transient Notion failures (5xx + 429). Three attempts with
# exponential backoff keeps total wall-time bounded (~7s worst case before
# honouring a Retry-After header) so the pipeline isn't blocked indefinitely.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 1.0


class NotionWriter:
    """Creates meeting summary pages in a Notion database."""

    def __init__(self, config: NotionConfig):
        self._config = config
        self._client: NotionClient | None = None
        # Set when a Notion API call fails (4xx or exhausted retries on 5xx).
        # The orchestrator emits a pipeline.warning so the UI can surface
        # "Notion output skipped: <reason>" rather than failing silently.
        self.last_error: str | None = None
        # Set after a successful write() to the created page's id. The
        # pipeline persists it on the meeting row so a reprocess can
        # archive the old page instead of accumulating duplicates.
        self.last_page_id: str | None = None

    def _get_client(self) -> NotionClient:
        """Lazy-initialise the Notion client."""
        if self._client is None:
            if not self._config.api_key:
                raise ValueError(
                    "Notion API key not set. Add it to config.yaml under notion.api_key."
                )
            self._client = NotionClient(auth=self._config.api_key)
        return self._client

    def _markdown_to_notion_blocks(self, markdown: str) -> list[dict]:
        """
        Convert a subset of Markdown into Notion block objects.

        Handles: H1, H2, H3, bullet lists, to-do items (- [ ]),
        paragraphs, and horizontal rules. This is intentionally
        simple; a full Markdown parser would be overkill for the
        structured output from the summariser.
        """
        blocks = []
        lines = markdown.split("\n")

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Headings
            if stripped.startswith("### "):
                blocks.append(self._heading_block(stripped[4:], level=3))
            elif stripped.startswith("## "):
                blocks.append(self._heading_block(stripped[3:], level=2))
            elif stripped.startswith("# "):
                blocks.append(self._heading_block(stripped[2:], level=1))

            # To-do items (action items)
            elif stripped.startswith("- [ ] "):
                blocks.append(self._todo_block(stripped[6:], checked=False))
            elif stripped.startswith("- [x] ") or stripped.startswith("- [X] "):
                blocks.append(self._todo_block(stripped[6:], checked=True))

            # Bullet list items
            elif stripped.startswith("- "):
                blocks.append(self._bullet_block(stripped[2:]))

            # Horizontal rules
            elif stripped in ("---", "***", "___"):
                blocks.append({"type": "divider", "divider": {}})

            # Everything else is a paragraph.
            else:
                blocks.append(self._paragraph_block(stripped))

        return blocks

    _NOTION_TEXT_LIMIT = 2000

    @staticmethod
    def _rich_text(text: str) -> list[dict]:
        """Wrap plain text in Notion's rich_text format, splitting if needed."""
        if len(text) <= NotionWriter._NOTION_TEXT_LIMIT:
            return [{"type": "text", "text": {"content": text}}]
        chunks = []
        for i in range(0, len(text), NotionWriter._NOTION_TEXT_LIMIT):
            chunks.append(
                {
                    "type": "text",
                    "text": {"content": text[i : i + NotionWriter._NOTION_TEXT_LIMIT]},
                }
            )
        return chunks

    def _heading_block(self, text: str, level: int) -> dict:
        heading_type = f"heading_{min(level, 3)}"
        return {
            "type": heading_type,
            heading_type: {"rich_text": self._rich_text(text)},
        }

    def _paragraph_block(self, text: str) -> dict:
        return {
            "type": "paragraph",
            "paragraph": {"rich_text": self._rich_text(text)},
        }

    def _bullet_block(self, text: str) -> dict:
        return {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": self._rich_text(text)},
        }

    def _todo_block(self, text: str, checked: bool = False) -> dict:
        return {
            "type": "to_do",
            "to_do": {
                "rich_text": self._rich_text(text),
                "checked": checked,
            },
        }

    @staticmethod
    def _retry_after_seconds(err: HTTPResponseError) -> float | None:
        """Extract a Retry-After delay (seconds) from an HTTP error, or None."""
        headers = getattr(err, "headers", None)
        if not headers:
            return None
        value = headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    def _call_with_retry(self, func: Callable[[], Any], description: str) -> Any:
        """Run a Notion client call with retry on 5xx/429.

        4xx responses (other than 429) raise ``APIResponseError`` and are
        re-raised immediately so the caller can stash them on ``last_error``.
        5xx responses retry with exponential backoff. 429 honours the
        ``Retry-After`` header when present.
        """
        last_exc: HTTPResponseError | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return func()
            except HTTPResponseError as e:
                status = getattr(e, "status", 0) or 0
                # 5xx or 429 are retryable; everything else bubbles immediately.
                if status < 500 and status != 429:
                    raise
                last_exc = e
                if attempt == _RETRY_ATTEMPTS:
                    break
                retry_after = self._retry_after_seconds(e) if status == 429 else None
                delay = (
                    retry_after
                    if retry_after is not None
                    else _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                )
                logger.warning(
                    "Notion %s failed (status=%s, attempt %d/%d); retrying in %.1fs",
                    description,
                    status,
                    attempt,
                    _RETRY_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        # Exhausted all attempts on a retryable error.
        assert last_exc is not None
        raise last_exc

    def write(
        self,
        summary: MeetingSummary,
        transcript: Transcript,
        started_at: float,
        duration_seconds: float,
    ) -> str | None:
        """
        Create a new page in the configured Notion database.

        Returns the URL of the created page, or ``None`` if the call failed
        with a 4xx error or exhausted its retries on a 5xx/429. In that case
        ``last_error`` is set with a human-readable message.
        """
        self.last_error = None
        self.last_page_id = None
        if not self._config.database_id:
            raise ValueError(
                "Notion database ID not set. Add it to config.yaml under notion.database_id."
            )
        client = self._get_client()
        props = self._config.properties
        date_str = time.strftime("%Y-%m-%d", time.localtime(started_at))

        # Build Notion database properties.
        properties = {
            props["title"]: {
                "title": self._rich_text(summary.title),
            },
            props["date"]: {
                "date": {"start": date_str},
            },
        }

        # Tags as multi-select (assumes the property exists and is multi-select).
        if summary.tags and props.get("tags"):
            properties[props["tags"]] = {
                "multi_select": [{"name": tag} for tag in summary.tags],
            }

        # Status (default to "Done" since the meeting is complete).
        if props.get("status"):
            properties[props["status"]] = {
                "status": {"name": "Done"},
            }

        # Convert summary markdown to Notion blocks.
        blocks = self._markdown_to_notion_blocks(summary.raw_markdown)

        # Notion API limits children to 100 blocks per request.
        # Batch if needed (unlikely for a meeting summary, but safe).
        children = blocks[:100]

        try:
            response = self._call_with_retry(
                lambda: client.pages.create(
                    parent={"database_id": self._config.database_id},
                    properties=properties,
                    children=children,
                ),
                description="pages.create",
            )
        except APIResponseError as e:
            # 4xx: a permanent client error (auth, validation, missing DB).
            # Stash so the orchestrator can surface it as a pipeline.warning.
            self.last_error = f"Notion API error ({getattr(e, 'status', '?')}): {e}"
            logger.error("Notion write failed: %s", self.last_error)
            return None
        except HTTPResponseError as e:
            # 5xx/429 retries exhausted.
            self.last_error = (
                f"Notion API unavailable (status {getattr(e, 'status', '?')}) "
                f"after {_RETRY_ATTEMPTS} attempts: {e}"
            )
            logger.error("Notion write failed: %s", self.last_error)
            return None

        page_url = response.get("url", "")
        page_id = response.get("id", "")
        self.last_page_id = page_id or None

        # Append remaining blocks if the summary exceeded 100.
        if len(blocks) > 100:
            logger.info(
                "Summary has %d blocks; appending in batches (Notion limit: 100 per request).",
                len(blocks),
            )
            for batch_num, i in enumerate(range(100, len(blocks), 100), start=2):
                batch = blocks[i : i + 100]
                try:
                    self._call_with_retry(
                        lambda batch=batch: client.blocks.children.append(
                            block_id=page_id,
                            children=batch,
                        ),
                        description="blocks.children.append",
                    )
                except APIResponseError as e:
                    self.last_error = (
                        f"Notion block append failed at batch {batch_num} "
                        f"({getattr(e, 'status', '?')}): {e}"
                    )
                    logger.error("Notion partial write: %s", self.last_error)
                    return page_url
                except HTTPResponseError as e:
                    self.last_error = (
                        f"Notion block append failed at batch {batch_num} "
                        f"after {_RETRY_ATTEMPTS} attempts "
                        f"(status {getattr(e, 'status', '?')}): {e}"
                    )
                    logger.error("Notion partial write: %s", self.last_error)
                    return page_url

        logger.info("Notion page created: %s", page_url)
        return page_url

    def archive_page(self, page_id: str) -> bool:
        """Archive a previously created page (best-effort).

        Used by reprocessing: the replacement page is written by a
        subsequent ``write()``, so the stale one is archived rather than
        left as a duplicate. Failures are logged, never raised — a page
        the user already deleted in Notion must not fail the pipeline.
        """
        if not page_id:
            return False
        try:
            client = self._get_client()
            self._call_with_retry(
                lambda: client.pages.update(page_id=page_id, archived=True),
                description="pages.update(archived)",
            )
        except Exception as e:
            logger.warning("Could not archive Notion page %s: %s", page_id, e)
            return False
        logger.info("Archived previous Notion page %s", page_id)
        return True
