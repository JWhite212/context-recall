"""Tests for src/calendar_permission.py — macOS Calendar (EventKit) TCC.

Mirrors src/mic_permission.py. The pure status mapping is exercised
directly; darwin calls are guarded and return UNKNOWN off-platform. No
test may trigger a real permission dialog."""

import sys

import pytest

from src import calendar_permission as cp
from src.calendar_permission import (
    AUTHORIZED,
    DENIED,
    NOT_DETERMINED,
    RESTRICTED,
    UNKNOWN,
    WRITE_ONLY,
    describe_fix,
    ensure_calendar_access,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, NOT_DETERMINED),
        (1, RESTRICTED),
        (2, DENIED),
        (3, AUTHORIZED),
        (4, WRITE_ONLY),
        (99, UNKNOWN),
    ],
)
def test_status_from_raw(raw, expected):
    assert cp._status_from_raw(raw) == expected


def test_authorization_status_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cp.authorization_status() == UNKNOWN


def test_ensure_calendar_access_authorized_has_no_problem(monkeypatch):
    monkeypatch.setattr(cp, "authorization_status", lambda: AUTHORIZED)
    status, problem = ensure_calendar_access()
    assert status == AUTHORIZED
    assert problem is None


@pytest.mark.parametrize("bad", [DENIED, RESTRICTED, WRITE_ONLY])
def test_ensure_calendar_access_denied_returns_problem(monkeypatch, bad):
    monkeypatch.setattr(cp, "authorization_status", lambda: bad)
    status, problem = ensure_calendar_access()
    assert status == bad
    assert problem is not None
    assert "System Settings" in problem


def test_describe_fix_not_determined_mentions_allow():
    assert "Allow" in describe_fix(NOT_DETERMINED)


def test_request_access_at_boot_returns_early_when_determined(monkeypatch):
    monkeypatch.setattr(cp, "authorization_status", lambda: AUTHORIZED)

    def _boom(**_kwargs):
        raise AssertionError("request_access must not be called when already determined")

    monkeypatch.setattr(cp, "request_access", _boom)
    assert cp.request_access_at_boot() == AUTHORIZED


def test_request_access_at_boot_requests_then_polls(monkeypatch):
    statuses = iter([NOT_DETERMINED, NOT_DETERMINED, AUTHORIZED])
    monkeypatch.setattr(cp, "authorization_status", lambda: next(statuses))
    called = {"n": 0}

    def _req(**_kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr(cp, "request_access", _req)
    monkeypatch.setattr(cp.time, "sleep", lambda *_a: None)
    result = cp.request_access_at_boot(timeout_seconds=100.0, poll_interval=0.0)
    assert result == AUTHORIZED
    assert called["n"] == 1


def test_boot_helper_delegates_to_module(monkeypatch):
    """main.py's boot helper must call request_access_at_boot and log,
    not reimplement polling."""
    import src.main as main_mod

    calls = {"n": 0}
    monkeypatch.setattr(
        main_mod.calendar_permission,
        "request_access_at_boot",
        lambda **_kw: calls.__setitem__("n", calls["n"] + 1) or AUTHORIZED,
    )
    # Unbound call with a bare namespace as `self` — the helper uses no
    # instance state.
    main_mod.ContextRecall._request_calendar_permission_at_boot(object())
    assert calls["n"] == 1
