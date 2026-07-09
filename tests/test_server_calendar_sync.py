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
        calendar=SimpleNamespace(import_enabled=import_enabled, sync_interval_minutes=15),
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
