"""Link/unlink service: forward + reverse link, adoption, move, conflict."""

import json

import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_link import (
    CalendarLinkConflict,
    link_meeting_to_event,
    unlink_meeting_from_event,
)
from src.db.database import Database
from src.db.repository import MeetingRepository


def _event(uid="EK1:1000"):
    return CalendarEvent(
        event_uid=uid,
        title="Quick Catch-Up",
        start_ts=1000.0,
        end_ts=2800.0,
        attendees=[{"name": "Jamie", "email": "j@x.com"}, {"name": "Amelia", "email": "a@x.com"}],
        organizer=None,
        join_url="https://teams.microsoft.com/l/meetup-join/x",
        meeting_id="19:mtg@thread.v2",
        calendar_name="Work",
    )


async def _fixture(tmp_path):
    db = Database(db_path=tmp_path / "cl.db")
    await db.connect()
    return db, MeetingRepository(db), CalendarEventRepository(db)


async def test_link_adopts_calendar_fields_and_sets_both_links(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        # A manual meeting title must be preserved.
        await mrepo.update_meeting(mid, title="Amelia Monthly Check-In", title_source="manual")
        meeting = await mrepo.get_meeting(mid)

        await link_meeting_to_event(mrepo, crepo, meeting, _event(), source="manual")

        m = await mrepo.get_meeting(mid)
        assert m.calendar_event_uid == "EK1:1000"
        assert m.calendar_event_title == "Quick Catch-Up"
        assert json.loads(m.attendees_json) == _event().attendees
        assert m.teams_join_url == _event().join_url
        assert m.teams_meeting_id == "19:mtg@thread.v2"
        assert m.calendar_confidence == 1.0
        assert m.title == "Amelia Monthly Check-In"  # preserved
        # Reverse link written + event mirrored.
        assert await mrepo.meeting_id_for_calendar_event("EK1:1000") == mid
        rows = await crepo.list_by_range(0.0, 5000.0)
        assert rows[0]["recorded_meeting_id"] == mid
    finally:
        await db.close()


async def test_relink_moves_and_clears_old_event(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        meeting = await mrepo.get_meeting(mid)
        await link_meeting_to_event(mrepo, crepo, meeting, _event("EK1:1000"))
        meeting = await mrepo.get_meeting(mid)
        await link_meeting_to_event(mrepo, crepo, meeting, _event("EK2:2000"))

        m = await mrepo.get_meeting(mid)
        assert m.calendar_event_uid == "EK2:2000"
        by_uid = {r["event_uid"]: r for r in await crepo.list_by_range(0.0, 5000.0)}
        assert by_uid["EK1:1000"]["recorded_meeting_id"] is None
        assert by_uid["EK2:2000"]["recorded_meeting_id"] == mid
    finally:
        await db.close()


async def test_link_conflict_when_event_linked_to_other_meeting(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        m1 = await mrepo.create_meeting(started_at=1005.0, status="complete")
        m2 = await mrepo.create_meeting(started_at=1010.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(m1), _event())
        with pytest.raises(CalendarLinkConflict):
            await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(m2), _event())
    finally:
        await db.close()


async def test_relink_same_event_is_idempotent(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        # Re-linking the SAME meeting to the SAME event must not 409.
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        assert (await mrepo.get_meeting(mid)).calendar_event_uid == "EK1:1000"
    finally:
        await db.close()


async def test_unlink_clears_both_sides(tmp_path):
    db, mrepo, crepo = await _fixture(tmp_path)
    try:
        mid = await mrepo.create_meeting(started_at=1005.0, status="complete")
        await link_meeting_to_event(mrepo, crepo, await mrepo.get_meeting(mid), _event())
        await unlink_meeting_from_event(mrepo, crepo, await mrepo.get_meeting(mid))
        assert (await mrepo.get_meeting(mid)).calendar_event_uid == ""
        rows = await crepo.list_by_range(0.0, 5000.0)
        assert rows[0]["recorded_meeting_id"] is None
    finally:
        await db.close()
