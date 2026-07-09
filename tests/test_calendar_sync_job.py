import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_events.sync import CalendarSyncJob


@pytest.fixture
async def cal_repo(db):
    return CalendarEventRepository(db)


def _ev(uid, start):
    return CalendarEvent(
        event_uid=uid,
        title="M",
        start_ts=start,
        end_ts=start + 1800.0,
        attendees=[{"name": "A"}, {"name": "B"}],
        organizer=None,
        join_url="",
        meeting_id="",
        calendar_name="Work",
    )


@pytest.mark.asyncio
async def test_apply_upserts_events(cal_repo):
    job = CalendarSyncJob(cal_repo)
    n = await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0), _ev("B:2000", 2000.0)])
    assert n == 2
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"A:1000", "B:2000"}


@pytest.mark.asyncio
async def test_apply_prunes_events_no_longer_present(cal_repo):
    job = CalendarSyncJob(cal_repo)
    await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0), _ev("B:2000", 2000.0)])
    # Second sync: B is gone (cancelled/moved).
    await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0)])
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"A:1000"}


@pytest.mark.asyncio
async def test_apply_does_not_prune_outside_window(cal_repo):
    job = CalendarSyncJob(cal_repo)
    await cal_repo.upsert(_ev("OLD:100", 100.0))  # before the window
    await job.apply(500.0, 10_000.0, [_ev("A:1000", 1000.0)])
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"OLD:100", "A:1000"}  # OLD untouched (outside sync window)
