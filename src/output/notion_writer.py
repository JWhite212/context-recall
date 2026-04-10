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

from notion_client import Client as NotionClient

from src.summariser import MeetingSummary
from src.transcriber import Transcript
from src.utils.config import NotionConfig

logger = logging.getLogger(__name__)


class NotionWriter:
    """Creates meeting summary pages in a Notion database."""

    def __init__(self, config: NotionConfig):
        self._config = config
        self._client: NotionClient | None = None

    def _get_client(self) -> NotionClient:
        """Lazy-initialise the Notion client."""
        if self._client is None:
            if not self._config.api_key:
                raise ValueError(
                    "Notion API key not set. Add it to config.yaml "
                    "under notion.api_key."
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
            chunks.append({
                "type": "text",
                "text": {"content": text[i:i + NotionWriter._NOTION_TEXT_LIMIT]},
            })
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

    def write(
        self,
        summary: MeetingSummary,
        transcript: Transcript,
        started_at: float,
        duration_seconds: float,
    ) -> str:
        """
        Create a new page in the configured Notion database.

        Returns the URL of the created page.
        """
        if not self._config.database_id:
            raise ValueError(
                "Notion database ID not set. Add it to config.yaml "
                "under notion.database_id."
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

        response = client.pages.create(
            parent={"database_id": self._config.database_id},
            properties=properties,
            children=children,
        )

        page_url = response.get("url", "")
        page_id = response.get("id", "")

        # Append remaining blocks if the summary exceeded 100.
        if len(blocks) > 100:
            logger.info(
                "Summary has %d blocks; appending in batches "
                "(Notion limit: 100 per request).",
                len(blocks),
            )
            for batch_num, i in enumerate(
                range(100, len(blocks), 100), start=2
            ):
                batch = blocks[i : i + 100]
                client.blocks.children.append(
                    block_id=page_id,
                    children=batch,
                )

        logger.info("Notion page created: %s", page_url)
        return page_url
