"""Tests for the calendar auto-arm controller (all collaborators faked)."""

from src.auto_arm import AutoArmController
from src.utils.config import AutoArmConfig


class FakeMonitor:
    def __init__(self, active=False):
        self.open = False
        self.start_calls = 0
        self.stop_calls = 0
        self._active = active

    def start(self):
        self.open = True
        self.start_calls += 1

    def stop(self):
        self.open = False
        self.stop_calls += 1

    def active(self):
        return self._active


def _event(end_ts=2800.0, uid="EK1:1000"):
    return {"event_uid": uid, "start_ts": 1000.0, "end_ts": end_ts, "join_url": "x"}


def _controller(**over):
    state = {
        "recording": over.pop("recording", False),
        "started": [],
        "stopped": 0,
    }
    monitor = over.pop("monitor", FakeMonitor())
    event = over.pop("event", None)
    process = over.pop("process_active", False)

    def start(ev):
        state["started"].append(ev)
        state["recording"] = True

    def stop():
        state["stopped"] += 1
        state["recording"] = False

    ctrl = AutoArmController(
        config=over.pop("config", AutoArmConfig(enabled=True)),
        calendar_source=lambda now, lead: event,
        audio_monitor=monitor,
        process_active=lambda: process,
        is_recording=lambda: state["recording"],
        start=start,
        stop=stop,
        clock=lambda: over.pop("clock_value", 1500.0),
    )
    return ctrl, state, monitor


def test_no_event_never_arms_or_starts():
    ctrl, state, monitor = _controller(event=None)
    ctrl.tick()
    assert monitor.open is False
    assert state["started"] == []


def test_armed_without_activity_opens_monitor_but_does_not_start():
    ctrl, state, monitor = _controller(event=_event())
    ctrl.tick()
    assert monitor.open is True
    assert monitor.start_calls == 1
    assert state["started"] == []


def test_audio_activity_starts_recording_and_closes_monitor():
    ctrl, state, monitor = _controller(event=_event(), monitor=FakeMonitor(active=True))
    ctrl.tick()
    assert len(state["started"]) == 1
    assert state["started"][0]["event_uid"] == "EK1:1000"
    assert monitor.open is False  # closed before capturing BlackHole


def test_process_activity_starts_recording():
    ctrl, state, monitor = _controller(event=_event(), process_active=True)
    ctrl.tick()
    assert len(state["started"]) == 1


def test_does_not_start_when_another_recording_is_active():
    # is_recording True but the controller never started it (recording=True at init).
    ctrl, state, monitor = _controller(
        event=_event(), monitor=FakeMonitor(active=True), recording=True
    )
    ctrl.tick()
    assert state["started"] == []
    assert monitor.open is False  # stays disarmed


def test_disarms_monitor_when_event_disappears():
    monitor = FakeMonitor()
    ctrl, state, _ = _controller(event=_event(), monitor=monitor)
    ctrl.tick()  # arms
    assert monitor.open is True
    ctrl._calendar_source = lambda now, lead: None  # event ends/moves away
    ctrl.tick()
    assert monitor.open is False
    assert monitor.stop_calls == 1


def test_stops_owned_recording_past_end_plus_trailing():
    # Start a recording, then advance the clock past end_ts + trailing (300s).
    clock = {"t": 1500.0}
    state = {"recording": False, "stopped": 0, "started": []}

    def start(ev):
        state["started"].append(ev)
        state["recording"] = True

    def stop():
        state["stopped"] += 1
        state["recording"] = False

    ctrl = AutoArmController(
        config=AutoArmConfig(enabled=True, trailing_minutes=5),
        calendar_source=lambda now, lead: _event(end_ts=2800.0),
        audio_monitor=FakeMonitor(active=True),
        process_active=lambda: False,
        is_recording=lambda: state["recording"],
        start=start,
        stop=stop,
        clock=lambda: clock["t"],
    )

    ctrl.tick()  # 1500 < 2800: arms + starts (audio active)
    assert state["started"] and state["stopped"] == 0

    clock["t"] = 3000.0  # 3000 < 2800 + 300 = 3100: not yet
    ctrl.tick()
    assert state["stopped"] == 0

    clock["t"] = 3200.0  # 3200 > 3100: stop
    ctrl.tick()
    assert state["stopped"] == 1


def test_does_not_stop_recording_it_did_not_start():
    ctrl, state, monitor = _controller(recording=True, event=_event(end_ts=100.0))
    # Owned-recording state was never set; clock (1500) is well past end+trailing.
    ctrl.tick()
    assert state["stopped"] == 0


def test_releases_owned_recording_when_ended_elsewhere():
    clock = {"t": 1500.0}
    state = {"recording": False, "stopped": 0, "started": []}

    def start(ev):
        state["started"].append(ev)
        state["recording"] = True

    def stop():
        state["stopped"] += 1
        state["recording"] = False

    ctrl = AutoArmController(
        config=AutoArmConfig(enabled=True),
        calendar_source=lambda now, lead: _event(),
        audio_monitor=FakeMonitor(active=True),
        process_active=lambda: False,
        is_recording=lambda: state["recording"],
        start=start,
        stop=stop,
        clock=lambda: clock["t"],
    )
    ctrl.tick()  # starts
    assert state["recording"] is True
    state["recording"] = False  # Teams-end / manual / silence watchdog stopped it
    ctrl.tick()  # controller releases its ownership without double-stopping
    assert state["stopped"] == 0
    # A fresh event can now arm again.
    ctrl.tick()
    assert len(state["started"]) == 2


def test_tick_swallows_calendar_source_exceptions():
    def boom(now, lead):
        raise RuntimeError("db down")

    ctrl = AutoArmController(
        config=AutoArmConfig(enabled=True),
        calendar_source=boom,
        audio_monitor=FakeMonitor(),
        process_active=lambda: False,
        is_recording=lambda: False,
        start=lambda ev: None,
        stop=lambda: None,
        clock=lambda: 1500.0,
    )
    ctrl.tick()  # must not raise
