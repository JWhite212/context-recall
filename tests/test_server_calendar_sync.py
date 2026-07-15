import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from src.api.server import ApiServer


class _RecordingScheduler:
    def __init__(self):
        self.registered = []

    def register(self, name, func, interval):
        self.registered.append((name, interval))


def _config(import_enabled=True):
    # Minimal config object with just the attributes _setup_scheduler_jobs touches.
    return SimpleNamespace(
        notifications=SimpleNamespace(enabled=False),
        analytics=SimpleNamespace(refresh_interval_hours=6),
        series=SimpleNamespace(heuristic_enabled=False),
        calendar=SimpleNamespace(
            import_enabled=import_enabled,
            sync_interval_minutes=15,
            sync_horizon_days=21,
            excluded_calendars=[],
        ),
        prep=SimpleNamespace(auto_generate=False, sweep_interval_minutes=15),
    )


def test_calendar_sync_job_registered_when_import_enabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(import_enabled=True)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "calendar_sync" in names
    interval = dict(server._scheduler.registered)["calendar_sync"]
    assert interval == 15 * 60


def test_calendar_sync_job_absent_when_import_disabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(import_enabled=False)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "calendar_sync" not in names


class _LazyReader:
    """`available` flips True only when list_events runs — like the real
    CalendarReader, whose lazy EventKit init happens inside list_events."""

    def __init__(self):
        self.available = False
        self.calls = 0

    def list_events(self, start, end, excluded_calendars=None):
        self.calls += 1
        self.available = True
        return []


class _RecordingSync:
    def __init__(self):
        self.applied = None

    async def apply(self, start, end, events):
        self.applied = (start, end, events)
        return 0


def test_sync_calendar_does_not_gate_on_available_before_lazy_init():
    """Regression (C1): gating on reader.available before list_events ever
    runs meant the scheduled sync never initialised the reader at all."""
    server = ApiServer()
    server._calendar_reader = _LazyReader()
    server._calendar_sync = _RecordingSync()
    with patch("src.api.server.load_config", return_value=_config()):
        asyncio.run(server._sync_calendar())
    assert server._calendar_reader.calls == 1
    assert server._calendar_sync.applied is not None


def test_sync_calendar_noop_without_reader_or_sync():
    server = ApiServer()
    server._calendar_reader = None
    server._calendar_sync = _RecordingSync()
    asyncio.run(server._sync_calendar())
    assert server._calendar_sync.applied is None


class _UnavailableReader:
    """Reader that stays unavailable even after list_events runs."""

    def __init__(self):
        self.available = False
        self.calls = 0

    def list_events(self, start, end, excluded_calendars=None):
        self.calls += 1
        return []


def test_sync_calendar_skips_apply_when_reader_stays_unavailable():
    """Regression: when reader is unavailable (e.g. grant revoked), list_events
    may be called for lazy init, but sync.apply must be skipped to avoid
    pruning the mirror. Only skip if unavailable AFTER list_events has run."""
    server = ApiServer()
    server._calendar_reader = _UnavailableReader()
    server._calendar_sync = _RecordingSync()
    with patch("src.api.server.load_config", return_value=_config()):
        asyncio.run(server._sync_calendar())
    assert server._calendar_reader.calls == 1
    assert server._calendar_sync.applied is None
