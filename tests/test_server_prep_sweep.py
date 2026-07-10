from types import SimpleNamespace
from unittest.mock import patch

from src.api.server import ApiServer


class _RecordingScheduler:
    def __init__(self):
        self.registered = []

    def register(self, name, func, interval):
        self.registered.append((name, interval))


def _config(auto=True, import_enabled=True):
    return SimpleNamespace(
        notifications=SimpleNamespace(enabled=False),
        analytics=SimpleNamespace(refresh_interval_hours=6),
        series=SimpleNamespace(heuristic_enabled=False),
        calendar=SimpleNamespace(import_enabled=import_enabled, sync_interval_minutes=15),
        prep=SimpleNamespace(auto_generate=auto, sweep_interval_minutes=15),
    )


def test_prep_sweep_registered_when_enabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(auto=True, import_enabled=True)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "prep_sweep" in names
    assert dict(server._scheduler.registered)["prep_sweep"] == 15 * 60


def test_prep_sweep_absent_when_auto_generate_off():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(auto=False, import_enabled=True)):
        server._setup_scheduler_jobs()
    assert "prep_sweep" not in [n for n, _ in server._scheduler.registered]
