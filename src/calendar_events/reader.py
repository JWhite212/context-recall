"""Read upcoming meeting-like events from macOS Calendar via EventKit.

Reuses the pure extraction helpers from src.calendar_matcher so the reactive
matcher and this range reader share one definition of attendee/Teams parsing.
All EventKit access is guarded: without EventKit (e.g. CI) the reader is simply
`available == False` and every read returns an empty list.
"""

import logging
import threading
from dataclasses import asdict, dataclass, field

from src.calendar_matcher import (
    _extract_attendee_info,
    _extract_teams_details,
    _is_eventkit_available,
)

logger = logging.getLogger("contextrecall.calendar_events")


@dataclass
class CalendarEvent:
    event_uid: str
    title: str
    start_ts: float
    end_ts: float
    attendees: list = field(default_factory=list)
    organizer: dict | None = None
    join_url: str = ""
    meeting_id: str = ""
    calendar_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_meeting_like(join_url: str, attendees: list) -> bool:
    """A calendar event counts as a meeting if it has a join link or >=2 attendees."""
    return bool(join_url) or len(attendees or []) >= 2


def _events_from_extracted(
    extracted: list[dict], excluded_calendars: set[str]
) -> list[CalendarEvent]:
    """Pure transform: filter extracted event dicts and build CalendarEvents.

    Skips all-day events, excluded calendars, and non-meeting-like events.
    The event_uid is synthesised as ``<eventIdentifier>:<int(start_ts)>`` because
    EventKit's eventIdentifier is shared across recurring occurrences.
    """
    events: list[CalendarEvent] = []
    for e in extracted:
        if e.get("is_all_day"):
            continue
        if e.get("calendar_name", "") in excluded_calendars:
            continue
        join_url = e.get("join_url", "") or ""
        attendees = e.get("attendees") or []
        if not is_meeting_like(join_url, attendees):
            continue
        start_ts = float(e["start_ts"])
        events.append(
            CalendarEvent(
                event_uid=f"{e['event_identifier']}:{int(start_ts)}",
                title=e.get("title", "") or "",
                start_ts=start_ts,
                end_ts=float(e.get("end_ts", start_ts)),
                attendees=attendees,
                organizer=e.get("organizer"),
                join_url=join_url,
                meeting_id=e.get("meeting_id", "") or "",
                calendar_name=e.get("calendar_name", "") or "",
            )
        )
    events.sort(key=lambda ev: ev.start_ts)
    return events


class CalendarReader:
    """Range reader over macOS Calendar events. EventKit access is lazy + guarded."""

    def __init__(self, excluded_calendars: list[str] | None = None) -> None:
        self._excluded = set(excluded_calendars or [])
        self._store = None
        self._authorized = False
        self._init_attempted = False
        self._init_lock = threading.Lock()

    def _ensure_store(self) -> None:
        """Lazily create the EventKit store and request access (blocking auth wait).

        Must be called from a worker thread (the API server offloads reads via
        run_in_executor), never on the event loop. The lock serialises
        concurrent first calls — two executor reads racing here used to both
        run the (up to 60s) auth wait; now losers block until the winner
        finishes and then return immediately.
        """
        with self._init_lock:
            if self._init_attempted:
                return
            self._init_attempted = True
            if not _is_eventkit_available():
                return
            try:
                import EventKit

                self._store = EventKit.EKEventStore.alloc().init()
                done = threading.Event()
                result = [False]

                def on_access(granted, error):
                    result[0] = granted
                    if error:
                        logger.warning("Calendar access error: %s", error)
                    done.set()

                self._store.requestAccessToEntityType_completion_(
                    EventKit.EKEntityTypeEvent, on_access
                )
                if done.wait(timeout=60):
                    self._authorized = result[0]
                else:
                    logger.warning("Calendar access request timed out")
            except Exception as e:  # pragma: no cover - requires EventKit
                logger.warning("Failed to initialise EventKit reader: %s", e)
                self._store = None

    @property
    def available(self) -> bool:
        return self._store is not None and self._authorized

    def _extract(self, event) -> dict | None:  # pragma: no cover - requires EventKit
        """Extract a plain dict from an EKEvent (the only EventKit-specific step)."""
        try:
            attendees = []
            raw = event.attendees()
            if raw:
                for p in raw:
                    info = _extract_attendee_info(p)
                    if not info:
                        continue
                    try:
                        if p.isCurrentUser():
                            continue
                    except Exception:
                        pass
                    attendees.append(info)
            organizer = None
            try:
                org = event.organizer()
                if org:
                    organizer = _extract_attendee_info(org)
            except Exception:
                pass
            join_url, meeting_id = "", ""
            for getter in (event.URL, event.notes, event.location):
                try:
                    val = getter()
                    if not val:
                        continue
                    text = str(val.absoluteString() if hasattr(val, "absoluteString") else val)
                    ju, mid = _extract_teams_details(text)
                    if ju:
                        join_url, meeting_id = ju, mid
                        break
                except Exception:
                    continue
            cal = ""
            try:
                cal = str(event.calendar().title() or "")
            except Exception:
                pass
            return {
                "event_identifier": str(event.eventIdentifier() or ""),
                "title": str(event.title() or ""),
                "start_ts": float(event.startDate().timeIntervalSince1970()),
                "end_ts": float(event.endDate().timeIntervalSince1970()),
                "attendees": attendees,
                "organizer": organizer,
                "join_url": join_url,
                "meeting_id": meeting_id,
                "calendar_name": cal,
                "is_all_day": bool(event.isAllDay()),
            }
        except Exception:
            return None

    def list_events(
        self, start: float, end: float, excluded_calendars: list[str] | None = None
    ) -> list[CalendarEvent]:
        """Return meeting-like events in [start, end). Empty if EventKit unavailable."""
        self._ensure_store()
        if not self.available:
            return []
        excluded = set(excluded_calendars) if excluded_calendars is not None else self._excluded
        try:  # pragma: no cover - requires EventKit
            from Foundation import NSDate

            ns_start = NSDate.dateWithTimeIntervalSince1970_(start)
            ns_end = NSDate.dateWithTimeIntervalSince1970_(end)
            predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                ns_start, ns_end, None
            )
            raw = self._store.eventsMatchingPredicate_(predicate) or []
            extracted = [x for x in (self._extract(e) for e in raw) if x]
            return _events_from_extracted(extracted, excluded)
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Calendar list_events failed: %s", e)
            return []

    def list_calendars(self) -> list[dict]:
        """Return [{id, title}] for every event calendar. Empty if unavailable."""
        self._ensure_store()
        if not self.available:
            return []
        try:  # pragma: no cover - requires EventKit
            import EventKit

            cals = self._store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []
            return [
                {"id": str(c.calendarIdentifier() or ""), "title": str(c.title() or "")}
                for c in cals
            ]
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Calendar list_calendars failed: %s", e)
            return []
