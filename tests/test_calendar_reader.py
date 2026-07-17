import sys
import types
from types import SimpleNamespace

from src.calendar_events.reader import (
    CalendarReader,
    _events_from_extracted,
    is_meeting_like,
)


def _fake_eventkit() -> types.ModuleType:
    """Minimal EventKit stand-in: enough for _ensure_store to alloc/init."""

    class _Store:
        pass

    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEventStore = SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: _Store()))
    return mod


def _eventkit_reader_env(monkeypatch, status):
    """Make EventKit 'importable' for the reader and pin the TCC status.

    `status` is a dict {"v": ...} so tests can flip it mid-test."""
    monkeypatch.setattr("src.calendar_events.reader._is_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit())
    monkeypatch.setattr("src.calendar_permission.authorization_status", lambda: status["v"])


def _extracted(**over):
    base = {
        "event_identifier": "EK1",
        "title": "Sync",
        "start_ts": 1000.0,
        "end_ts": 2000.0,
        "attendees": [{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@x.com"}],
        "organizer": None,
        "join_url": "",
        "meeting_id": "",
        "calendar_name": "Work",
        "is_all_day": False,
    }
    base.update(over)
    return base


def test_is_meeting_like():
    assert is_meeting_like("https://teams...", []) is True
    assert is_meeting_like("", [{"name": "A"}, {"name": "B"}]) is True
    assert is_meeting_like("", [{"name": "A"}]) is False
    assert is_meeting_like("", []) is False


def test_event_uid_synthesised_from_identifier_and_start():
    events = _events_from_extracted([_extracted()], set())
    assert len(events) == 1
    assert events[0].event_uid == "EK1:1000"
    assert events[0].to_dict()["event_uid"] == "EK1:1000"


def test_all_day_events_skipped():
    assert _events_from_extracted([_extracted(is_all_day=True)], set()) == []


def test_non_meeting_like_skipped():
    assert _events_from_extracted([_extracted(attendees=[{"name": "A"}], join_url="")], set()) == []


def test_excluded_calendar_skipped():
    assert _events_from_extracted([_extracted(calendar_name="Personal")], {"Personal"}) == []


def test_events_sorted_by_start():
    out = _events_from_extracted(
        [
            _extracted(event_identifier="B", start_ts=3000.0),
            _extracted(event_identifier="A", start_ts=1000.0),
        ],
        set(),
    )
    assert [e.start_ts for e in out] == [1000.0, 3000.0]


def test_reader_unavailable_returns_empty_without_eventkit(monkeypatch):
    monkeypatch.setattr("src.calendar_events.reader._is_eventkit_available", lambda: False)
    reader = CalendarReader()
    # In CI EventKit is unavailable, so the reader is not available.
    assert reader.available is False
    assert reader.list_events(0.0, 10_000.0) == []
    assert reader.list_calendars() == []


def test_reader_already_authorized_skips_blocking_request(monkeypatch):
    """When macOS already reports the grant, the store must be created
    directly — no 60s blocking request. The conftest guard raises if
    request_access fires, so passing proves the fast path."""
    _eventkit_reader_env(monkeypatch, {"v": "authorized"})
    reader = CalendarReader()
    reader._ensure_store()
    assert reader.available is True


def test_reader_retries_after_unanswered_request(monkeypatch):
    """Regression (I1): a not-yet-granted/timed-out request must NOT set
    the permanent init latch — the next call (e.g. the sync tick after the
    boot poller obtains the grant) must retry and succeed."""
    status = {"v": "not_determined"}
    _eventkit_reader_env(monkeypatch, status)
    requests = {"n": 0}

    def _unanswered(**_kw):
        requests["n"] += 1
        return None  # dialog not answered within the timeout

    monkeypatch.setattr("src.calendar_permission.request_access", _unanswered)
    reader = CalendarReader()
    reader._ensure_store()
    assert reader.available is False
    assert requests["n"] == 1

    # The boot poller (or the user) has now granted access.
    status["v"] = "authorized"
    reader._ensure_store()
    assert reader.available is True
    assert requests["n"] == 1  # fast path — no second request


def test_reader_request_granted_initialises_and_latches(monkeypatch):
    status = {"v": "not_determined"}
    _eventkit_reader_env(monkeypatch, status)
    requests = {"n": 0}

    def _granted(**_kw):
        requests["n"] += 1
        return True

    monkeypatch.setattr("src.calendar_permission.request_access", _granted)
    reader = CalendarReader()
    reader._ensure_store()
    assert reader.available is True
    reader._ensure_store()  # latched — no further request
    assert requests["n"] == 1


def test_reader_not_determined_requests_are_cooldown_bounded(monkeypatch):
    """B1: while status stays not_determined, back-to-back reads must NOT
    re-fire request_access on every call — that is what leaks EKEventStore
    instances (EKCADErrorDomain 1021). A cooldown bounds the retries."""
    status = {"v": "not_determined"}
    _eventkit_reader_env(monkeypatch, status)
    requests = {"n": 0}

    def _unanswered(**_kw):
        requests["n"] += 1
        return None

    monkeypatch.setattr("src.calendar_permission.request_access", _unanswered)
    reader = CalendarReader()
    reader._ensure_store()
    reader._ensure_store()
    reader._ensure_store()
    # Three consecutive not-determined reads, but the cooldown collapses them
    # to a single request.
    assert requests["n"] == 1
    assert reader.available is False


def test_reader_denied_never_requests_and_heals_on_settings_grant(monkeypatch):
    """A determined-but-blocked status (denied) cannot prompt, so no
    request may fire (the conftest guard raises if it does) — and it must
    not latch: granting later in System Settings self-heals."""
    status = {"v": "denied"}
    _eventkit_reader_env(monkeypatch, status)
    reader = CalendarReader()
    reader._ensure_store()
    assert reader.available is False

    status["v"] = "authorized"
    reader._ensure_store()
    assert reader.available is True
