"""Tests for src/calendar_permission.py — macOS Calendar (EventKit) TCC.

Mirrors src/mic_permission.py. The pure status mapping is exercised
directly; darwin calls are guarded and return UNKNOWN off-platform. No
test may trigger a real permission dialog."""

import sys
import types
from types import SimpleNamespace

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

# Captured at import time (collection), BEFORE the conftest autouse guard
# replaces them — lets tests of the real implementations bypass the guard
# without weakening it for the rest of the suite.
_REAL_AUTHORIZATION_STATUS = cp.authorization_status
_REAL_REQUEST_ACCESS = cp.request_access


def _fake_eventkit(store) -> types.ModuleType:
    """A minimal stand-in for the EventKit module: enough for
    request_access() to alloc/init a store and name the entity type."""
    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEventStore = SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: store))
    return mod


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
    assert _REAL_AUTHORIZATION_STATUS() == UNKNOWN


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


def test_get_shared_store_is_a_singleton(monkeypatch):
    """B1: the whole process must reuse ONE EKEventStore. Repeated calls
    return the same instance and alloc/init exactly once — creating a fresh
    store per call is what exhausts EventKit (EKCADErrorDomain 1021)."""
    allocs = {"n": 0}

    class _Store:
        pass

    def _alloc():
        allocs["n"] += 1
        return SimpleNamespace(init=lambda: _Store())

    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEventStore = SimpleNamespace(alloc=_alloc)
    monkeypatch.setattr(cp, "_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", mod)

    cp.reset_shared_store()
    first = cp.get_shared_store()
    second = cp.get_shared_store()
    assert first is not None
    assert first is second
    assert allocs["n"] == 1


def test_get_shared_store_none_without_eventkit(monkeypatch):
    monkeypatch.setattr(cp, "_eventkit_available", lambda: False)
    cp.reset_shared_store()
    assert cp.get_shared_store() is None


def test_request_access_reuses_shared_store(monkeypatch):
    """request_access must draw from the shared store rather than allocating
    its own — otherwise every boot/retry leaks a store."""
    allocs = {"n": 0}

    class ModernStore:
        def requestFullAccessToEventsWithCompletion_(self, handler):
            handler(True, None)

    def _alloc():
        allocs["n"] += 1
        return SimpleNamespace(init=lambda: ModernStore())

    mod = types.ModuleType("EventKit")
    mod.EKEntityTypeEvent = 0
    mod.EKEventStore = SimpleNamespace(alloc=_alloc)
    monkeypatch.setattr(cp, "_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", mod)

    cp.reset_shared_store()
    assert _REAL_REQUEST_ACCESS(timeout_seconds=1.0) is True
    assert _REAL_REQUEST_ACCESS(timeout_seconds=1.0) is True
    assert allocs["n"] == 1  # both requests shared one store


def test_request_access_prefers_full_access_api(monkeypatch):
    """macOS 14 split calendar access: the legacy entity-type request only
    grants write-only there. Prefer requestFullAccessToEventsWithCompletion_
    when the store exposes it."""
    calls: list[str] = []

    class ModernStore:
        def requestFullAccessToEventsWithCompletion_(self, handler):
            calls.append("full")
            handler(True, None)

        def requestAccessToEntityType_completion_(self, entity, handler):
            calls.append("legacy")
            handler(True, None)

    monkeypatch.setattr(cp, "_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit(ModernStore()))
    assert _REAL_REQUEST_ACCESS(timeout_seconds=1.0) is True
    assert calls == ["full"]


def test_request_access_falls_back_to_legacy_api(monkeypatch):
    calls: list[str] = []

    class LegacyStore:
        def requestAccessToEntityType_completion_(self, entity, handler):
            calls.append("legacy")
            handler(False, None)

    monkeypatch.setattr(cp, "_eventkit_available", lambda: True)
    monkeypatch.setitem(sys.modules, "EventKit", _fake_eventkit(LegacyStore()))
    assert _REAL_REQUEST_ACCESS(timeout_seconds=1.0) is False
    assert calls == ["legacy"]


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
