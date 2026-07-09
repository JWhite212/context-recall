from src.calendar_events.reader import (
    CalendarReader,
    _events_from_extracted,
    is_meeting_like,
)


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
