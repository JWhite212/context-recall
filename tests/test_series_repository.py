"""Tests for SeriesRepository."""

import pytest

from src.db.database import Database
from src.series.repository import SeriesRepository


@pytest.fixture
async def series_repo(db: Database):
    return SeriesRepository(db)


@pytest.mark.asyncio
async def test_create_and_get_series(series_repo):
    series_id = await series_repo.create(title="Weekly Standup", detection_method="manual")
    series = await series_repo.get(series_id)
    assert series is not None
    assert series["title"] == "Weekly Standup"
    assert series["detection_method"] == "manual"


@pytest.mark.asyncio
async def test_list_series(series_repo):
    await series_repo.create(title="Series A", detection_method="heuristic")
    await series_repo.create(title="Series B", detection_method="calendar")
    items = await series_repo.list_all()
    assert len(items) == 2


@pytest.mark.asyncio
async def test_update_series(series_repo):
    series_id = await series_repo.create(title="Old Title", detection_method="manual")
    await series_repo.update(series_id, title="New Title")
    series = await series_repo.get(series_id)
    assert series["title"] == "New Title"


@pytest.mark.asyncio
async def test_link_meeting_to_series(series_repo, repo):
    series_id = await series_repo.create(title="Recurring", detection_method="manual")
    meeting_id = await repo.create_meeting(started_at=1700000000)
    await series_repo.link_meeting(meeting_id, series_id)
    meetings = await series_repo.get_meetings(series_id)
    assert len(meetings) == 1
    assert meetings[0]["id"] == meeting_id


@pytest.mark.asyncio
async def test_unlink_meeting(series_repo, repo):
    series_id = await series_repo.create(title="Recurring", detection_method="manual")
    meeting_id = await repo.create_meeting(started_at=1700000000)
    await series_repo.link_meeting(meeting_id, series_id)
    await series_repo.unlink_meeting(meeting_id)
    meetings = await series_repo.get_meetings(series_id)
    assert len(meetings) == 0


@pytest.mark.asyncio
async def test_find_by_calendar_series_id(series_repo):
    await series_repo.create(
        title="Sprint Retro", detection_method="calendar", calendar_series_id="cal-abc-123"
    )
    found = await series_repo.find_by_calendar_id("cal-abc-123")
    assert found is not None
    assert found["title"] == "Sprint Retro"
    not_found = await series_repo.find_by_calendar_id("nonexistent")
    assert not_found is None


@pytest.mark.asyncio
async def test_delete_series_unlinks_meetings(series_repo, repo):
    series_id = await series_repo.create(title="Temp", detection_method="manual")
    meeting_id = await repo.create_meeting(started_at=1700000000)
    await series_repo.link_meeting(meeting_id, series_id)
    await series_repo.delete(series_id)
    # Series should be gone
    assert await series_repo.get(series_id) is None
    # Meeting should still exist with series_id = NULL
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.series_id is None


@pytest.mark.asyncio
async def test_create_with_all_optional_fields(series_repo):
    series_id = await series_repo.create(
        title="Full Series",
        detection_method="calendar",
        calendar_series_id="cal-xyz",
        typical_attendees_json='["alice", "bob"]',
        typical_day_of_week=2,
        typical_time="14:00",
        typical_duration_minutes=30,
    )
    series = await series_repo.get(series_id)
    assert series["calendar_series_id"] == "cal-xyz"
    assert series["typical_attendees_json"] == '["alice", "bob"]'
    assert series["typical_day_of_week"] == 2
    assert series["typical_time"] == "14:00"
    assert series["typical_duration_minutes"] == 30


@pytest.mark.asyncio
async def test_get_nonexistent_series(series_repo):
    result = await series_repo.get("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_get_meetings_empty(series_repo):
    series_id = await series_repo.create(title="Empty", detection_method="manual")
    meetings = await series_repo.get_meetings(series_id)
    assert meetings == []
