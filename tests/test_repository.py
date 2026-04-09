"""Tests for MeetingRepository CRUD operations."""

import time

import pytest

from src.db.repository import MeetingRepository


@pytest.mark.asyncio
async def test_create_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    assert meeting_id
    assert len(meeting_id) == 36  # UUID format


@pytest.mark.asyncio
async def test_get_meeting(repo: MeetingRepository):
    now = time.time()
    meeting_id = await repo.create_meeting(started_at=now)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.id == meeting_id
    assert meeting.started_at == now
    assert meeting.status == "recording"


@pytest.mark.asyncio
async def test_get_nonexistent_meeting(repo: MeetingRepository):
    meeting = await repo.get_meeting("nonexistent-id")
    assert meeting is None


@pytest.mark.asyncio
async def test_update_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(meeting_id, title="Test Meeting", status="complete")
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.title == "Test Meeting"
    assert meeting.status == "complete"


@pytest.mark.asyncio
async def test_update_meeting_invalid_field(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    with pytest.raises(ValueError, match="Cannot update"):
        await repo.update_meeting(meeting_id, id="hacked")


@pytest.mark.asyncio
async def test_update_meeting_tags(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(meeting_id, tags=["standup", "team"])
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.tags == ["standup", "team"]


@pytest.mark.asyncio
async def test_list_meetings_pagination(repo: MeetingRepository):
    now = time.time()
    for i in range(5):
        await repo.create_meeting(started_at=now + i)

    page1 = await repo.list_meetings(limit=3, offset=0)
    page2 = await repo.list_meetings(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 2
    # Newest first
    assert page1[0].started_at > page1[2].started_at


@pytest.mark.asyncio
async def test_list_meetings_status_filter(repo: MeetingRepository):
    now = time.time()
    id1 = await repo.create_meeting(started_at=now, status="recording")
    id2 = await repo.create_meeting(started_at=now + 1, status="complete")

    recording = await repo.list_meetings(status="recording")
    complete = await repo.list_meetings(status="complete")
    assert len(recording) == 1
    assert recording[0].id == id1
    assert len(complete) == 1
    assert complete[0].id == id2


@pytest.mark.asyncio
async def test_delete_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    deleted = await repo.delete_meeting(meeting_id)
    assert deleted is True
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is None


@pytest.mark.asyncio
async def test_delete_nonexistent_meeting(repo: MeetingRepository):
    deleted = await repo.delete_meeting("nonexistent")
    assert deleted is False


@pytest.mark.asyncio
async def test_count_meetings(repo: MeetingRepository):
    assert await repo.count_meetings() == 0
    await repo.create_meeting(started_at=time.time(), status="complete")
    await repo.create_meeting(started_at=time.time(), status="recording")
    assert await repo.count_meetings() == 2
    assert await repo.count_meetings(status="complete") == 1


@pytest.mark.asyncio
async def test_search_meetings_by_title(repo: MeetingRepository):
    """Search finds meetings by title via FTS or LIKE fallback."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(
        mid,
        title="Sprint Planning Review",
        status="complete",
        summary_markdown="Discussed the sprint backlog items.",
    )
    # FTS content-sync may not work in test since transcript_text isn't a
    # real column in meetings. Manually populate FTS index.
    try:
        await repo._db.conn.execute(
            "INSERT INTO meetings_fts (rowid, title, summary_markdown, transcript_text) "
            "SELECT rowid, title, summary_markdown, '' FROM meetings WHERE id = ?",
            (mid,),
        )
        await repo._db.conn.commit()
    except Exception:
        pass  # FTS may not be available

    results = await repo.search_meetings("Sprint")
    assert len(results) >= 1
    assert results[0].title == "Sprint Planning Review"


@pytest.mark.asyncio
async def test_search_meetings_no_results(repo: MeetingRepository):
    await repo.create_meeting(started_at=time.time())
    results = await repo.search_meetings("nonexistent query xyz")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_cleanup_old_meetings(repo: MeetingRepository, tmp_path):
    # Create an old meeting (91 days ago)
    old_time = time.time() - (91 * 86400)
    old_id = await repo.create_meeting(started_at=old_time)

    # Create a recent meeting
    await repo.create_meeting(started_at=time.time())

    result = await repo.cleanup_old_meetings(
        audio_retention_days=0,
        record_retention_days=90,
    )
    assert result["records_deleted"] == 1

    # Old meeting gone, recent one still exists
    assert await repo.get_meeting(old_id) is None
    assert await repo.count_meetings() == 1
