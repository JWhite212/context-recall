"""apply_rename: DB update + propagation + event, all best-effort."""

import asyncio

import pytest

from src.meeting_rename import apply_rename
from src.utils.config import AppConfig, MarkdownConfig, NotionConfig


class _Bus:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)


@pytest.fixture
def disabled_writers_config() -> AppConfig:
    """A config with both output writers disabled, so propagation is a no-op."""
    return AppConfig(markdown=MarkdownConfig(enabled=False), notion=NotionConfig(enabled=False))


@pytest.mark.asyncio
async def test_apply_rename_updates_db_and_emits(repo, disabled_writers_config):
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title="Auto", title_source="auto")
    meeting = await repo.get_meeting(mid)
    bus = _Bus()

    out = await apply_rename(
        repo,
        meeting,
        "Manual Name",
        config=disabled_writers_config,
        event_bus=bus,
        loop=asyncio.get_running_loop(),
    )
    assert out == {"meeting_id": mid, "title": "Manual Name", "title_source": "manual"}
    m = await repo.get_meeting(mid)
    assert m.title == "Manual Name" and m.title_source == "manual"
    assert bus.events == [{"type": "meeting.renamed", "meeting_id": mid, "title": "Manual Name"}]


@pytest.mark.asyncio
async def test_apply_rename_no_event_bus_does_not_raise(repo, disabled_writers_config):
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    meeting = await repo.get_meeting(mid)

    out = await apply_rename(
        repo,
        meeting,
        "No Bus Name",
        config=disabled_writers_config,
        event_bus=None,
        loop=asyncio.get_running_loop(),
    )
    assert out["title"] == "No Bus Name"
    assert out["title_source"] == "manual"
