"""Link/unlink a recorded meeting to a calendar entry.

Shared by the manual API endpoint (bidirectional) and — for the forward
link only — the auto-link path at record time. Kept pure over the two
repositories so it is unit-testable against a real SQLite DB.
"""

import json
import logging

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.db.repository import MeetingRecord, MeetingRepository

logger = logging.getLogger("contextrecall.calendar_link")


class CalendarLinkConflict(Exception):
    """The target calendar event is already linked to another recording."""


async def link_meeting_to_event(
    meeting_repo: MeetingRepository,
    calendar_event_repo: CalendarEventRepository,
    meeting: MeetingRecord,
    event: CalendarEvent,
    *,
    source: str = "manual",
) -> None:
    """Link ``meeting`` to ``event``: forward link + adopt calendar-derived
    fields + (best-effort) reverse link. Moves an existing link. Raises
    ``CalendarLinkConflict`` if the event is already tied to another meeting.
    """
    owner = await meeting_repo.meeting_id_for_calendar_event(event.event_uid)
    if owner and owner != meeting.id:
        raise CalendarLinkConflict(
            f"Calendar event {event.event_uid} is already linked to meeting {owner}"
        )

    old_uid = meeting.calendar_event_uid or ""

    # Forward link + adopt calendar-derived fields. User-authored fields
    # (title, tags, assignment, speakers) are deliberately untouched.
    await meeting_repo.update_meeting(
        meeting.id,
        calendar_event_uid=event.event_uid,
        calendar_event_title=event.title,
        attendees_json=json.dumps(event.attendees or []),
        teams_join_url=event.join_url,
        teams_meeting_id=event.meeting_id,
        calendar_confidence=1.0,
    )

    # Reverse link (best-effort): mirror the event, then mark it recorded.
    try:
        await calendar_event_repo.upsert(event)
        await calendar_event_repo.set_recorded_meeting(event.event_uid, meeting.id)
        if old_uid and old_uid != event.event_uid:
            await calendar_event_repo.set_recorded_meeting(old_uid, None)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Reverse calendar link failed for meeting %s → %s (%s): %s",
            meeting.id,
            event.event_uid,
            source,
            e,
        )


async def unlink_meeting_from_event(
    meeting_repo: MeetingRepository,
    calendar_event_repo: CalendarEventRepository,
    meeting: MeetingRecord,
) -> None:
    """Clear the meeting's forward link and the event's reverse link."""
    old_uid = meeting.calendar_event_uid or ""
    await meeting_repo.update_meeting(meeting.id, calendar_event_uid="")
    if old_uid:
        try:
            await calendar_event_repo.set_recorded_meeting(old_uid, None)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Reverse unlink failed for event %s: %s", old_uid, e)
