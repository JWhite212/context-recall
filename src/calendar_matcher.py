"""
Calendar integration via macOS EventKit.

Matches detected meetings to calendar events by Teams meeting URL
or time-window proximity, extracting event titles and attendee names.
All processing is local -- no cloud API calls.

Requires macOS and the pyobjc-framework-EventKit package.
"""

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from src import calendar_permission

logger = logging.getLogger("contextrecall.calendar")

# Teams meeting URL pattern.
TEAMS_URL_PATTERN = re.compile(
    r"https://teams\.microsoft\.com/l/meetup-join/"
    r"([^\s/]+)"  # thread_id
    r"/(\d+)"  # timestamp
    r"\?context=([^\s\"']+)",  # context JSON (URL-encoded)
    re.IGNORECASE,
)


@dataclass
class CalendarMatch:
    """Result of matching a meeting to a calendar event."""

    event_title: str
    attendees: list[dict] = field(default_factory=list)  # [{"name": "...", "email": "..."}]
    organizer: dict | None = None  # {"name": "...", "email": "..."}
    confidence: float = 0.0
    match_method: str = "none"  # "teams_url" | "time_window"
    event_start: float = 0.0
    event_end: float = 0.0
    teams_join_url: str = ""
    teams_meeting_id: str = ""


def _is_eventkit_available() -> bool:
    """Check if EventKit framework is available."""
    try:
        import EventKit  # noqa: F401

        return True
    except ImportError:
        return False


def _extract_teams_thread_id(text: str) -> str | None:
    """Extract Teams meeting thread ID from text containing a Teams URL."""
    if not text:
        return None
    match = TEAMS_URL_PATTERN.search(text)
    if match:
        return unquote(match.group(1))
    return None


def _extract_teams_details(text: str) -> tuple[str, str]:
    """Extract full Teams join URL and meeting thread ID from text.

    Returns (join_url, meeting_id). Both may be empty strings on failure.
    The meeting_id is the decoded thread ID which uniquely identifies the
    Teams meeting (e.g. "19:meeting_ZmE2...@thread.v2").
    """
    if not text:
        return "", ""
    match = TEAMS_URL_PATTERN.search(text)
    if not match:
        return "", ""
    join_url = match.group(0)
    meeting_id = unquote(match.group(1))
    return join_url, meeting_id


def _score_time_match(event_start: float, event_end: float, meeting_start: float) -> float:
    """Score how well a meeting start time aligns with a calendar event.

    Returns 0.0 to 1.0.
    """
    delta = abs(meeting_start - event_start)

    # Perfect: within 5 minutes of event start
    if delta <= 300:
        return 0.95 - (delta / 300) * 0.15  # 0.80 to 0.95

    # Good: meeting started during the event window
    if event_start <= meeting_start <= event_end:
        return 0.70

    # Acceptable: within 15 minutes before event start (joined early)
    if event_start - 900 <= meeting_start < event_start:
        return 0.60

    return 0.0


def _extract_attendee_info(participant) -> dict | None:
    """Extract name and email from an EKParticipant."""
    try:
        name = ""
        email = ""

        # Try to get the name
        try:
            name = str(participant.name() or "")
        except Exception:
            pass

        # Try to get email from URL (format: mailto:user@example.com)
        try:
            url = participant.URL()
            if url:
                url_str = str(url.absoluteString() if hasattr(url, "absoluteString") else url)
                if "mailto:" in url_str.lower():
                    email = url_str.split("mailto:", 1)[-1].split("?")[0].strip()
        except Exception:
            pass

        if not name and not email:
            return None

        return {"name": name, "email": email}
    except Exception:
        return None


class CalendarMatcher:
    """Matches detected meetings to macOS calendar events."""

    def __init__(self, time_window_minutes: int = 15, min_confidence: float = 0.7):
        self._time_window = time_window_minutes * 60  # Convert to seconds
        self._min_confidence = min_confidence
        self._store = None
        self._authorized = False

        if not _is_eventkit_available():
            logger.warning("EventKit not available; calendar matching disabled")
            return

        self._init_store()

    def _init_store(self) -> None:
        """Initialise the EventKit store, requesting access only when needed.

        When macOS already reports the grant (boot poller answered, or a
        previous run was authorized) the store is created directly — no 60s
        blocking wait at daemon boot. Only a genuinely undetermined status
        fires the (blocking, may-prompt) request.
        """
        try:
            status = calendar_permission.authorization_status()
            if status == calendar_permission.AUTHORIZED:
                self._create_store()
                if self.available:
                    logger.info("Calendar access already granted")
                return
            if status in (
                calendar_permission.DENIED,
                calendar_permission.RESTRICTED,
                calendar_permission.WRITE_ONLY,
            ):
                # Determined-but-blocked: requesting cannot prompt. match()
                # self-heals once the user grants access in System Settings.
                logger.warning(
                    "Calendar access blocked (%s); matching disabled until granted", status
                )
                return
            # NOT_DETERMINED (or UNKNOWN introspection failure): fire the
            # request — blocks until the user answers the dialog or the
            # timeout elapses. Prefers the macOS 14+ full-access API.
            granted = calendar_permission.request_access(timeout_seconds=60.0)
            if granted:
                self._create_store()
                logger.info("Calendar access granted")
            elif granted is False:
                logger.warning("Calendar access denied by user")
            else:
                logger.warning("Calendar access request timed out or unavailable")
        except Exception as e:
            logger.warning("Failed to initialize EventKit: %s", e)

    def _create_store(self) -> None:
        """Create the EKEventStore for an already-authorized process."""
        try:
            import EventKit

            self._store = EventKit.EKEventStore.alloc().init()
            self._authorized = True
        except Exception as e:
            logger.warning("Failed to initialize EventKit: %s", e)

    def _retry_init(self) -> None:
        """Late-grant self-heal: when authorization arrived after
        construction (boot poller answered, System Settings), create the
        store now. Never prompts — one cheap status check per attempt."""
        if not _is_eventkit_available():
            return
        if calendar_permission.authorization_status() != calendar_permission.AUTHORIZED:
            return
        self._create_store()
        if self.available:
            logger.info("Calendar access granted after start — matching enabled")

    @property
    def available(self) -> bool:
        return self._store is not None and self._authorized

    def match(self, meeting_started_at: float) -> CalendarMatch | None:
        """Match a detected meeting to a calendar event.

        Args:
            meeting_started_at: Unix timestamp when the meeting was detected.

        Returns:
            CalendarMatch if a match is found above min_confidence, else None.
        """
        if not self.available:
            self._retry_init()
        if not self.available:
            return None

        try:
            return self._do_match(meeting_started_at)
        except Exception as e:
            logger.warning("Calendar matching failed: %s", e)
            return None

    def _do_match(self, meeting_started_at: float) -> CalendarMatch | None:
        """Internal matching logic."""
        from Foundation import NSDate

        # Query events in a time window around the meeting start.
        ns_start = NSDate.dateWithTimeIntervalSince1970_(meeting_started_at - self._time_window)
        ns_end = NSDate.dateWithTimeIntervalSince1970_(meeting_started_at + self._time_window)

        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_start,
            ns_end,
            None,  # None = search all calendars
        )
        events = self._store.eventsMatchingPredicate_(predicate)

        if not events:
            logger.debug("No calendar events found in time window")
            return None

        candidates = []
        for event in events:
            # Skip all-day events
            if event.isAllDay():
                continue

            event_start = float(event.startDate().timeIntervalSince1970())
            event_end = float(event.endDate().timeIntervalSince1970())
            title = str(event.title() or "")

            # Tier 1: Teams URL match
            teams_thread_id = None
            join_url = ""
            meeting_id = ""
            for field_getter in [event.URL, event.notes, event.location]:
                try:
                    field_value = field_getter()
                    if field_value:
                        text = str(
                            field_value.absoluteString()
                            if hasattr(field_value, "absoluteString")
                            else field_value
                        )
                        tid = _extract_teams_thread_id(text)
                        if tid:
                            teams_thread_id = tid
                            join_url, meeting_id = _extract_teams_details(text)
                            break
                except Exception:
                    continue

            # Extract attendees
            attendees = []
            organizer_info = None

            try:
                raw_attendees = event.attendees()
                if raw_attendees:
                    for p in raw_attendees:
                        info = _extract_attendee_info(p)
                        if info:
                            try:
                                if p.isCurrentUser():
                                    continue  # Skip self
                            except Exception:
                                pass
                            attendees.append(info)
            except Exception:
                pass

            try:
                org = event.organizer()
                if org:
                    organizer_info = _extract_attendee_info(org)
            except Exception:
                pass

            if teams_thread_id:
                # Tier 1: Teams URL match -- highest confidence
                candidates.append(
                    CalendarMatch(
                        event_title=title,
                        attendees=attendees,
                        organizer=organizer_info,
                        confidence=1.0,
                        match_method="teams_url",
                        event_start=event_start,
                        event_end=event_end,
                        teams_join_url=join_url,
                        teams_meeting_id=meeting_id,
                    )
                )
            else:
                # Tier 2: Time-window match
                score = _score_time_match(event_start, event_end, meeting_started_at)
                if score >= self._min_confidence:
                    candidates.append(
                        CalendarMatch(
                            event_title=title,
                            attendees=attendees,
                            organizer=organizer_info,
                            confidence=score,
                            match_method="time_window",
                            event_start=event_start,
                            event_end=event_end,
                        )
                    )

        if not candidates:
            return None

        # Return the highest confidence match.
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        best = candidates[0]
        logger.info(
            "Calendar match: '%s' (confidence=%.0f%%, method=%s, attendees=%d)",
            best.event_title,
            best.confidence * 100,
            best.match_method,
            len(best.attendees),
        )
        return best
