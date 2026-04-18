"""Tests for calendar matching logic (unit tests that don't require EventKit)."""

from src.calendar_matcher import (
    CalendarMatch,
    _extract_teams_thread_id,
    _score_time_match,
)


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
