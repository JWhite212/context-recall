import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository


@pytest.fixture
async def cal_repo(db):
    return CalendarEventRepository(db)


def _ev(uid="EK1:1000", start=1000.0, title="Sync"):
    return CalendarEvent(
        event_uid=uid,
        title=title,
        start_ts=start,
        end_ts=start + 1800.0,
        attendees=[{"name": "A", "email": "a@x.com"}],
        organizer=None,
        join_url="https://teams",
        meeting_id="19:abc",
        calendar_name="Work",
    )


@pytest.mark.asyncio
async def test_upsert_and_list_by_range(cal_repo):
    await cal_repo.upsert(_ev())
    rows = await cal_repo.list_by_range(0.0, 10_000.0)
    assert len(rows) == 1
    assert rows[0]["event_uid"] == "EK1:1000"
    assert rows[0]["attendees"] == [{"name": "A", "email": "a@x.com"}]
    assert rows[0]["join_url"] == "https://teams"


@pytest.mark.asyncio
async def test_upsert_updates_existing_but_preserves_recorded_link(cal_repo):
    await cal_repo.upsert(_ev(title="Sync"))
    await cal_repo.set_recorded_meeting("EK1:1000", "m1")
    await cal_repo.upsert(_ev(title="Renamed"))  # re-sync same uid
    rows = await cal_repo.list_by_range(0.0, 10_000.0)
    assert rows[0]["title"] == "Renamed"
    assert rows[0]["recorded_meeting_id"] == "m1"  # not clobbered by upsert


@pytest.mark.asyncio
async def test_list_by_range_excludes_out_of_window(cal_repo):
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    await cal_repo.upsert(_ev(uid="EK2:9000", start=9000.0))
    rows = await cal_repo.list_by_range(0.0, 5000.0)
    assert [r["event_uid"] for r in rows] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_prune_window_removes_absent_but_keeps_recorded(cal_repo):
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    await cal_repo.upsert(_ev(uid="EK2:2000", start=2000.0))
    await cal_repo.set_recorded_meeting("EK2:2000", "m2")
    # Only EK1 is still present in the fresh fetch; EK2 vanished but is recorded.
    removed = await cal_repo.prune_window(0.0, 5000.0, keep_uids={"EK1:1000"})
    assert removed == 0  # EK2 kept because it has a recorded_meeting_id
    await cal_repo.upsert(_ev(uid="EK3:3000", start=3000.0))
    removed = await cal_repo.prune_window(0.0, 5000.0, keep_uids={"EK1:1000"})
    assert removed == 1  # EK3 pruned; EK2 still kept
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 5000.0)}
    assert rows == {"EK1:1000", "EK2:2000"}
