"""Apply a meeting rename: DB update + best-effort output propagation +
meeting.renamed event. Shared by the PATCH endpoint. Propagation failures
are logged and surfaced but never fail the rename — the DB update is the
source of truth."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("contextrecall.meeting_rename")


def _propagate(meeting, new_title: str, config) -> str | None:
    """Rename the Obsidian note + update the Notion page title (blocking).
    Runs off the event loop. Returns the new markdown path if the file was
    renamed, else None."""
    new_markdown_path: str | None = None
    if getattr(config.markdown, "enabled", False) and meeting.markdown_path:
        try:
            from src.output.markdown_writer import MarkdownWriter

            writer = MarkdownWriter(config.markdown)
            result = writer.rename_note(Path(meeting.markdown_path), new_title, meeting.started_at)
            if result is not None:
                new_markdown_path = str(result)
        except Exception as e:
            logger.warning("Markdown rename failed: %s", e)

    if getattr(config.notion, "enabled", False) and meeting.notion_page_id:
        try:
            from src.output.notion_writer import NotionWriter

            NotionWriter(config.notion).update_page_title(meeting.notion_page_id, new_title)
        except Exception as e:
            logger.warning("Notion title update failed: %s", e)

    return new_markdown_path


async def apply_rename(repo, meeting, new_title: str, *, config, event_bus, loop) -> dict[str, Any]:
    """Set the title as manual, propagate to outputs, emit meeting.renamed."""
    await repo.update_meeting(meeting.id, title=new_title, title_source="manual")

    # Propagation is blocking I/O — run it off the event loop.
    new_md_path = await asyncio.get_running_loop().run_in_executor(
        None, _propagate, meeting, new_title, config
    )
    if new_md_path and new_md_path != meeting.markdown_path:
        await repo.update_meeting(meeting.id, markdown_path=new_md_path)

    if event_bus is not None:
        event_bus.emit({"type": "meeting.renamed", "meeting_id": meeting.id, "title": new_title})

    return {"meeting_id": meeting.id, "title": new_title, "title_source": "manual"}
