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


@pytest.mark.asyncio
async def test_delete_extracted_for_meeting_spares_manual_items(ai_repo, meeting_id):
    """Reprocess replaces the previous extraction round; the user's
    manually created items must survive untouched."""
    await ai_repo.create(meeting_id=meeting_id, title="Extracted A", source="extracted")
    await ai_repo.create(meeting_id=meeting_id, title="Extracted B", source="extracted")
    manual_id = await ai_repo.create(meeting_id=meeting_id, title="Manual", source="manual")

    deleted = await ai_repo.delete_extracted_for_meeting(meeting_id)

    assert deleted == 2
    remaining = await ai_repo.list_by_meeting(meeting_id)
    assert [i["id"] for i in remaining] == [manual_id]


@pytest.mark.asyncio
async def test_delete_extracted_scoped_to_meeting(ai_repo, meeting_id, repo):
    other = await repo.create_meeting(started_at=1700000100)
    await ai_repo.create(meeting_id=meeting_id, title="Mine", source="extracted")
    await ai_repo.create(meeting_id=other, title="Theirs", source="extracted")

    await ai_repo.delete_extracted_for_meeting(meeting_id)

    assert await ai_repo.list_by_meeting(meeting_id) == []
    assert len(await ai_repo.list_by_meeting(other)) == 1


@pytest.mark.asyncio
async def test_delete_extracted_preserves_manual_tags(ai_repo, meeting_id):
    """A user's manual client/project tag on an extracted item (set via
    PATCH, tag_source='manual') must survive reprocess even though the
    item's ``source`` stays 'extracted' — only the untagged/inherited
    extracted rows get replaced."""
    keep = await ai_repo.create(meeting_id=meeting_id, title="tagged", source="extracted")
    await ai_repo.update(keep, client_id="c1", tag_source="manual")
    drop = await ai_repo.create(meeting_id=meeting_id, title="plain", source="extracted")

    deleted = await ai_repo.delete_extracted_for_meeting(meeting_id)

    assert deleted == 1
    assert await ai_repo.get(keep) is not None
    assert await ai_repo.get(drop) is None


@pytest.mark.asyncio
async def test_create_and_update_tags(ai_repo, meeting_id):
    item_id = await ai_repo.create(
        meeting_id=meeting_id, title="Ship it", client_id="c1", project_id="p1"
    )
    item = await ai_repo.get(item_id)
    assert item["client_id"] == "c1"
    assert item["project_id"] == "p1"
    assert item["tag_source"] == "inherited"

    await ai_repo.update(item_id, client_id="c2", tag_source="manual")
    item = await ai_repo.get(item_id)
    assert item["client_id"] == "c2"
    assert item["tag_source"] == "manual"


@pytest.mark.asyncio
async def test_list_items_filters_by_client_and_priority(ai_repo, meeting_id):
    await ai_repo.create(meeting_id=meeting_id, title="a", priority="high", client_id="c1")
    await ai_repo.create(meeting_id=meeting_id, title="b", priority="low", client_id="c1")
    await ai_repo.create(meeting_id=meeting_id, title="c", priority="high", client_id="c2")

    got = await ai_repo.list_items(client_id="c1", priority="high")
    assert [i["title"] for i in got] == ["a"]


@pytest.mark.asyncio
async def test_list_items_filters_by_project_and_due_after(ai_repo, meeting_id):
    await ai_repo.create(meeting_id=meeting_id, title="x", project_id="p1", due_date="2026-08-01")
    await ai_repo.create(meeting_id=meeting_id, title="y", project_id="p1", due_date="2026-01-01")
    await ai_repo.create(meeting_id=meeting_id, title="z", project_id="p2", due_date="2026-08-01")

    got = await ai_repo.list_items(project_id="p1", due_after="2026-06-01")
    assert [i["title"] for i in got] == ["x"]
