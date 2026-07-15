"""Tests for calendar matching logic (unit tests that don't require EventKit)."""

import sys
import types
from types import SimpleNamespace

from src.calendar_matcher import (
    CalendarMatch,
    CalendarMatcher,
    _extract_teams_thread_id,
    _score_time_match,
)


def _fake_eventkit() -> types.ModuleType:
    """Minimal EventKit stand-in: enough for the matcher to alloc/init."""

    class _Store:
        pass

    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEventStore = SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: _Store()))
    return mod


def _eventkit_matcher_env(monkeypatch, status):
    monkeypatch.setattr("src.calendar_matcher._is_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit())
    monkeypatch.setattr("src.calendar_permission.authorization_status", lambda: status["v"])


def test_extract_teams_url_from_notes():
    text = (
        "Join: https://teams.microsoft.com/l/meetup-join/"
        "19%3ameeting_abc123%40thread.v2/0?context=%7B%22Tid%22%3A%22tid123%22%7D"
    )
    thread_id = _extract_teams_thread_id(text)
    assert thread_id is not None
    assert "meeting_abc123" in thread_id


def test_extract_teams_url_no_match():
    assert _extract_teams_thread_id("Just a regular meeting") is None
    assert _extract_teams_thread_id("") is None
    assert _extract_teams_thread_id(None) is None


def test_score_time_match_perfect():
    # Meeting starts exactly when event starts
    score = _score_time_match(1000.0, 2000.0, 1000.0)
    assert score >= 0.90


def test_score_time_match_close():
    # Meeting starts 3 minutes after event
    score = _score_time_match(1000.0, 2000.0, 1180.0)
    assert 0.80 <= score <= 0.95


def test_score_time_match_during():
    # Meeting starts 10 minutes into event
    score = _score_time_match(1000.0, 4600.0, 1600.0)
    assert score >= 0.60


def test_score_time_match_early():
    # Meeting starts 10 minutes before event
    score = _score_time_match(1600.0, 4600.0, 1000.0)
    assert score >= 0.50


def test_score_time_match_no_match():
    # Meeting starts 30 minutes after event ends
    score = _score_time_match(1000.0, 2000.0, 3800.0)
    assert score == 0.0


def test_calendar_match_dataclass():
    """Verify CalendarMatch can be instantiated with defaults."""
    match = CalendarMatch(event_title="Weekly Standup")
    assert match.event_title == "Weekly Standup"
    assert match.attendees == []
    assert match.organizer is None
    assert match.confidence == 0.0
    assert match.match_method == "none"


def test_matcher_already_authorized_skips_blocking_request(monkeypatch):
    """When macOS already reports the grant (boot poller, previous run),
    construction must create the store directly — no 60s blocking wait at
    daemon boot. The conftest guard raises if request_access fires, so
    passing proves the fast path."""
    _eventkit_matcher_env(monkeypatch, {"v": "authorized"})
    matcher = CalendarMatcher()
    assert matcher.available is True


def test_matcher_self_heals_on_late_grant(monkeypatch):
    """Regression (I1): a matcher built before the boot prompt was answered
    must not be dead forever — once the grant lands, match() re-initialises."""
    status = {"v": "not_determined"}
    _eventkit_matcher_env(monkeypatch, status)
    monkeypatch.setattr("src.calendar_permission.request_access", lambda **_kw: None)
    matcher = CalendarMatcher()
    assert matcher.available is False

    status["v"] = "authorized"
    matcher.match(1000.0)  # fake store has no events; the heal still runs
    assert matcher.available is True


def test_matcher_denied_never_requests_and_heals_on_settings_grant(monkeypatch):
    """A determined denied status cannot prompt, so no request may fire
    (the conftest guard raises if it does) — and a later System Settings
    grant self-heals via match()."""
    status = {"v": "denied"}
    _eventkit_matcher_env(monkeypatch, status)
    matcher = CalendarMatcher()
    assert matcher.available is False

    status["v"] = "authorized"
    matcher.match(1000.0)
    assert matcher.available is True
