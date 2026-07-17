"""Read upcoming meeting-like events from macOS Calendar via EventKit.

Reuses the pure extraction helpers from src.calendar_matcher so the reactive
matcher and this range reader share one definition of attendee/Teams parsing.
All EventKit access is guarded: without EventKit (e.g. CI) the reader is simply
`available == False` and every read returns an empty list.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

from src import calendar_permission
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
    calendar_id: str = ""

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
    Exclusion is keyed by calendar **id** so two distinct calendars sharing a
    title stay independently toggleable; a calendar *name* in the excluded set
    is also honoured for backward compatibility with pre-id configs.
    The event_uid is synthesised as ``<eventIdentifier>:<int(start_ts)>`` because
    EventKit's eventIdentifier is shared across recurring occurrences.
    """
    events: list[CalendarEvent] = []
    seen: set = set()
    for e in extracted:
        if e.get("is_all_day"):
            continue
        cal_name = e.get("calendar_name", "") or ""
        cal_id = e.get("calendar_identifier", "") or ""
        if cal_id in excluded_calendars or cal_name in excluded_calendars:
            continue
        join_url = e.get("join_url", "") or ""
        attendees = e.get("attendees") or []
        if not is_meeting_like(join_url, attendees):
            continue
        start_ts = float(e["start_ts"])
        end_ts = float(e.get("end_ts", start_ts))
        title = e.get("title", "") or ""
        meeting_id = e.get("meeting_id", "") or ""
        # Collapse the same meeting appearing on multiple calendars (duplicate
        # accounts, or a mirrored invite) to a single row. Prefer the strongest
        # identity available; fall back to title + time window.
        if meeting_id:
            key = ("mid", meeting_id)
        elif join_url:
            key = ("url", join_url)
        else:
            key = ("tt", title.strip().lower(), int(start_ts), int(end_ts))
        if key in seen:
            continue
        seen.add(key)
        events.append(
            CalendarEvent(
                event_uid=f"{e['event_identifier']}:{int(start_ts)}",
                title=title,
                start_ts=start_ts,
                end_ts=end_ts,
                attendees=attendees,
                organizer=e.get("organizer"),
                join_url=join_url,
                meeting_id=meeting_id,
                calendar_name=cal_name,
                calendar_id=cal_id,
            )
        )
    events.sort(key=lambda ev: ev.start_ts)
    return events


class CalendarReader:
    """Range reader over macOS Calendar events. EventKit access is lazy + guarded."""

    # While the status is NOT_DETERMINED the store is unlatched so a later
    # grant self-heals — but firing a fresh 60s request on every read leaks
    # EKEventStore instances (via request_access). Bound retries to one per
    # this many seconds.
    _REQUEST_COOLDOWN_SECONDS = 30.0

    def __init__(self, excluded_calendars: list[str] | None = None) -> None:
        self._excluded = set(excluded_calendars or [])
        self._store = None
        self._authorized = False
        self._init_attempted = False
        self._init_lock = threading.Lock()
        self._last_request_ts = 0.0

    def _ensure_store(self) -> None:
        """Lazily create the EventKit store, requesting access only when needed.

        Must be called from a worker thread (the API server offloads reads via
        run_in_executor), never on the event loop — the access request can
        block for up to 60s. The lock serialises concurrent first calls.

        Initialisation only latches (``_init_attempted``) once it SUCCEEDS,
        or when EventKit itself is missing (which cannot change within a
        process). A not-yet-granted or timed-out request leaves the reader
        unlatched so a later call — e.g. the next scheduled sync tick after
        the boot poller obtains the grant — retries and succeeds.
        """
        with self._init_lock:
            if self._init_attempted:
                return
            if not _is_eventkit_available():
                self._init_attempted = True
                return
            status = calendar_permission.authorization_status()
            if status == calendar_permission.AUTHORIZED:
                # Already granted (boot poller, System Settings, a previous
                # run): create the store WITHOUT the blocking request.
                self._create_store()
                self._init_attempted = True
                return
            if status in (
                calendar_permission.DENIED,
                calendar_permission.RESTRICTED,
                calendar_permission.WRITE_ONLY,
            ):
                # Determined-but-blocked: requesting cannot prompt. Do not
                # latch — a later grant in System Settings self-heals on
                # the next call.
                return
            # NOT_DETERMINED (or UNKNOWN introspection failure): fire the
            # request, but at most once per cooldown window. Re-requesting on
            # every read (UI polls, sync tick, auto-arm) is what exhausts
            # EventKit. The boot poller is the primary requester; this is the
            # self-heal backstop.
            now = time.monotonic()
            if now - self._last_request_ts < self._REQUEST_COOLDOWN_SECONDS:
                return
            self._last_request_ts = now
            if calendar_permission.request_access(timeout_seconds=60.0):
                self._create_store()
                self._init_attempted = True

    def _create_store(self) -> None:
        """Attach the process-wide shared EKEventStore for an already-authorized
        process. Reusing the singleton (rather than alloc()'ing a fresh store)
        is what keeps the daemon under macOS's EKEventStore instance cap."""
        store = calendar_permission.get_shared_store()
        if store is None:  # pragma: no cover - requires EventKit
            logger.warning("Failed to initialise EventKit reader: no shared store")
            self._store = None
            self._authorized = False
            return
        self._store = store
        self._authorized = True

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
            cal_id = ""
            try:
                calendar = event.calendar()
                cal = str(calendar.title() or "")
                cal_id = str(calendar.calendarIdentifier() or "")
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
                "calendar_identifier": cal_id,
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
        """Return [{id, title, source}] for every event calendar. ``source`` is
        the account name (iCloud, Google, …) so the picker can disambiguate two
        calendars that share a title. Empty if unavailable."""
        self._ensure_store()
        if not self.available:
            return []
        try:  # pragma: no cover - requires EventKit
            import EventKit

            cals = self._store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []
            result = []
            for c in cals:
                source = ""
                try:
                    src = c.source()
                    if src:
                        source = str(src.title() or "")
                except Exception:
                    pass
                result.append(
                    {
                        "id": str(c.calendarIdentifier() or ""),
                        "title": str(c.title() or ""),
                        "source": source,
                    }
                )
            return result
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Calendar list_calendars failed: %s", e)
            return []
