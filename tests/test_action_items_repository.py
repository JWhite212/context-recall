"""Tests for ActionItemRepository CRUD operations."""

import pytest

from src.action_items.repository import ActionItemRepository
from src.db.database import Database
from src.db.repository import MeetingRepository


@pytest.fixture
async def ai_repo(db: Database):
    return ActionItemRepository(db)


@pytest.fixture
async def meeting_id(repo: MeetingRepository):
    return await repo.create_meeting(started_at=1700000000)


@pytest.mark.asyncio
async def test_create_and_get(ai_repo, meeting_id):
    item_id = await ai_repo.create(
        meeting_id=meeting_id, title="Write tests", assignee="Alice", priority="high"
    )
    item = await ai_repo.get(item_id)
    assert item["title"] == "Write tests"
    assert item["assignee"] == "Alice"
    assert item["status"] == "open"
    assert item["priority"] == "high"


@pytest.mark.asyncio
async def test_update_status(ai_repo, meeting_id):
    item_id = await ai_repo.create(meeting_id=meeting_id, title="Task")
    await ai_repo.update(item_id, status="in_progress")
    item = await ai_repo.get(item_id)
    assert item["status"] == "in_progress"


@pytest.mark.asyncio
async def test_update_status_to_done_sets_completed_at(ai_repo, meeting_id):
    item_id = await ai_repo.create(meeting_id=meeting_id, title="Task")
    await ai_repo.update(item_id, status="done")
    item = await ai_repo.get(item_id)
    assert item["status"] == "done"
    assert item["completed_at"] is not None


@pytest.mark.asyncio
async def test_reopen_clears_completed_at(ai_repo, meeting_id):
    item_id = await ai_repo.create(meeting_id=meeting_id, title="Task")
    await ai_repo.update(item_id, status="done")
    await ai_repo.update(item_id, status="open")
    item = await ai_repo.get(item_id)
    assert item["status"] == "open"
    assert item["completed_at"] is None


@pytest.mark.asyncio
async def test_list_by_status(ai_repo, meeting_id):
    await ai_repo.create(meeting_id=meeting_id, title="Open 1")
    item2 = await ai_repo.create(meeting_id=meeting_id, title="Done 1")
    await ai_repo.update(item2, status="done")
    open_items = await ai_repo.list_items(status="open")
    assert len(open_items) == 1
    assert open_items[0]["title"] == "Open 1"


@pytest.mark.asyncio
async def test_list_by_meeting(ai_repo, meeting_id, repo):
    other_meeting = await repo.create_meeting(started_at=1700000100)
    await ai_repo.create(meeting_id=meeting_id, title="Item A")
    await ai_repo.create(meeting_id=other_meeting, title="Item B")
    items = await ai_repo.list_by_meeting(meeting_id)
    assert len(items) == 1
    assert items[0]["title"] == "Item A"


@pytest.mark.asyncio
async def test_list_overdue(ai_repo, meeting_id):
    await ai_repo.create(meeting_id=meeting_id, title="Overdue", due_date="2020-01-01")
    await ai_repo.create(meeting_id=meeting_id, title="Future", due_date="2099-01-01")
    overdue = await ai_repo.list_overdue()
    assert len(overdue) == 1
    assert overdue[0]["title"] == "Overdue"


@pytest.mark.asyncio
async def test_delete(ai_repo, meeting_id):
    item_id = await ai_repo.create(meeting_id=meeting_id, title="Delete me")
    await ai_repo.delete(item_id)
    assert await ai_repo.get(item_id) is None
