# Auto-Arm Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-start recording for scheduled join-link calendar meetings — arm during the event window, start on real meeting activity (system-audio RMS **or** a meeting-app process using audio), and stop at the event's end.

**Architecture:** A new `AutoArmController` (`src/auto_arm.py`) is ticked from the existing `TeamsDetector` poll loop via a new `on_tick` hook. It resolves the current armed join-link event from the foundation's `calendar_events` mirror, opens a BlackHole-only `AudioMonitor` (`src/audio_monitor.py`) while armed-and-not-recording, and drives the orchestrator's existing recording start/stop. All collaborators are injected, so the controller and monitor are unit-tested with fakes — no real audio, EventKit, or DB. Opt-in, off by default.

**Tech Stack:** Python 3 (dataclasses, `sounddevice`, `numpy`, `aiosqlite`, `asyncio.run_coroutine_threadsafe`), React 19 + TypeScript + TanStack Query + Vitest.

## Global Constraints

- **Opt-in, off by default.** `auto_arm.enabled = False`. The controller is only constructed/ticked when `config.auto_arm.enabled AND config.calendar.import_enabled` are both true.
- **`AutoArmConfig` defaults (verbatim):** `enabled: bool = False`, `lead_minutes: int = 2`, `trailing_minutes: int = 5`, `activity_rms_dbfs: float = -45.0`, `activity_sustain_seconds: int = 3`, `meeting_process_names: list[str] = ["zoom.us", "Microsoft Teams", "Google Chrome"]` (via `field(default_factory=lambda: [...])`).
- **The poll thread must never be crashed by auto-arm.** `AutoArmController.tick()` swallows and logs all exceptions. The detector's `on_tick` invocation is _also_ wrapped defensively.
- **Only query the calendar while NOT recording.** The controller reads the calendar source only for the arm decision (idle state); once a recording it owns is in flight, the stop decision uses the event captured at start — no fresh query. This keeps the bounded `run_coroutine_threadsafe(...).result(timeout=2.0)` bridge call safe (the API loop is idle when we're not recording).
- **BlackHole is resolved by substring match**, exactly like `AudioCapture._find_device` (case-insensitive `name.lower() in device["name"].lower()` with `max_input_channels > 0`). Do **not** use the mic-side resolvers (`resolve_default_mic_index` / `is_virtual_input`) — they deliberately exclude BlackHole.
- **BlackHole stream is stereo.** Open `sd.InputStream(..., channels=2, dtype="float32", blocksize=1024)`; downmix to mono via `np.mean(indata, axis=1)` before computing RMS.
- **dBFS floor:** `-100.0` for `rms < 1e-10`, else `20.0 * log10(rms)` (matches `AudioCapture._rms_dbfs`).
- **Config convention = exactly 3 edits** in `src/utils/config.py` (dataclass, `AppConfig` field, `load_config` line), mirroring `AutomationsConfig`.
- **Never double-trigger.** Recording start/stop funnel through the orchestrator's existing single-capture path; `is_recording` guards it. The controller only stops recordings _it_ started.

---

## File Structure

**Create:**

- `src/auto_arm.py` — `AutoArmController` (pure orchestration logic, injected collaborators).
- `src/audio_monitor.py` — `AudioMonitor` (BlackHole-only RMS watcher; pure sustain core + guarded stream I/O).
- `tests/test_auto_arm.py` — controller tests (all fakes).
- `tests/test_audio_monitor.py` — monitor sustain-core tests (no real stream).
- `ui/src/components/settings/AutoArmSection.tsx` — Settings toggle (config GET/PUT).
- `ui/src/components/settings/__tests__/AutoArmSection.test.tsx` — toggle test.

**Modify:**

- `src/utils/config.py` — add `AutoArmConfig` (3 edit sites).
- `config.example.yaml` — document the `auto_arm` section.
- `src/calendar_events/repository.py` — add `current_join_link_event`.
- `tests/test_calendar_event_repository.py` — extend for the new read method.
- `src/detector.py` — add `on_tick` hook + `app_using_audio` passthrough.
- `tests/test_detector.py` — test the `on_tick` hook.
- `src/main.py` — construct + wire the controller (daemon boot).
- `tests/test_orchestrator.py` — test the wiring helper.
- `ui/src/lib/types.ts` — add `AutoArmConfig` + `auto_arm` on `AppConfig`.
- `ui/src/components/settings/Settings.tsx` — render `AutoArmSection` + nav entry.

---

## Task 1: `AutoArmConfig` (Python config)

**Files:**

- Modify: `src/utils/config.py` (after `AutomationsConfig` ~line 247; `AppConfig` field ~line 368; `load_config` ~line 474)
- Modify: `config.example.yaml` (after the `automations:` block ~line 285)
- Test: `tests/test_config.py`

**Interfaces:**

- Produces: `AutoArmConfig` dataclass with fields `enabled: bool`, `lead_minutes: int`, `trailing_minutes: int`, `activity_rms_dbfs: float`, `activity_sustain_seconds: int`, `meeting_process_names: list[str]`; accessible as `AppConfig.auto_arm`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_auto_arm_config_defaults(tmp_path):
    """auto_arm section defaults to off with the documented values."""
    from src.utils.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("detection:\n  poll_interval_seconds: 3\n")
    cfg = load_config(path)

    assert cfg.auto_arm.enabled is False
    assert cfg.auto_arm.lead_minutes == 2
    assert cfg.auto_arm.trailing_minutes == 5
    assert cfg.auto_arm.activity_rms_dbfs == -45.0
    assert cfg.auto_arm.activity_sustain_seconds == 3
    assert cfg.auto_arm.meeting_process_names == [
        "zoom.us",
        "Microsoft Teams",
        "Google Chrome",
    ]


def test_auto_arm_config_overrides_from_yaml(tmp_path):
    """Values in config.yaml override the dataclass defaults."""
    from src.utils.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "auto_arm:\n"
        "  enabled: true\n"
        "  lead_minutes: 5\n"
        "  meeting_process_names:\n"
        "    - zoom.us\n"
    )
    cfg = load_config(path)

    assert cfg.auto_arm.enabled is True
    assert cfg.auto_arm.lead_minutes == 5
    assert cfg.auto_arm.meeting_process_names == ["zoom.us"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_auto_arm_config_defaults -v`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'auto_arm'`.

- [ ] **Step 3: Add the dataclass (edit site 1)**

In `src/utils/config.py`, immediately after the `AutomationsConfig` dataclass (the block ending around line 248), add:

```python
@dataclass
class AutoArmConfig:
    """Calendar-driven auto-start recording for scheduled join-link meetings."""

    enabled: bool = False
    lead_minutes: int = 2
    trailing_minutes: int = 5
    activity_rms_dbfs: float = -45.0
    activity_sustain_seconds: int = 3
    meeting_process_names: list[str] = field(
        default_factory=lambda: ["zoom.us", "Microsoft Teams", "Google Chrome"]
    )
```

- [ ] **Step 4: Add the `AppConfig` field (edit site 2)**

In the `AppConfig` dataclass, directly after the `automations: AutomationsConfig = field(default_factory=AutomationsConfig)` line (~line 368), add:

```python
    auto_arm: AutoArmConfig = field(default_factory=AutoArmConfig)
```

- [ ] **Step 5: Add the `load_config` line (edit site 3)**

In `load_config`'s `AppConfig(...)` construction, directly after the `automations=_build_dataclass(AutomationsConfig, raw.get("automations", {})),` line (~line 474), add:

```python
        auto_arm=_build_dataclass(AutoArmConfig, raw.get("auto_arm", {})),
```

- [ ] **Step 6: Document the section in `config.example.yaml`**

In `config.example.yaml`, after the `automations:` block (the commented `# enabled: true` line ~285) and before the `# --- Recurring Meeting Detection ---` comment, insert:

```yaml
# --- Auto-Arm Recording (Track B) ---
# Auto-start recording for scheduled join-link calendar meetings. Requires
# calendar import (calendar.import_enabled). Off by default — it records
# without an explicit user action, so opt in deliberately.
auto_arm:
  # Master switch.
  enabled: false

  # Arm this many minutes before the event's scheduled start.
  # lead_minutes: 2

  # Keep recording this many minutes past the event's scheduled end.
  # trailing_minutes: 5

  # System-audio RMS (dBFS) that counts as "meeting activity".
  # activity_rms_dbfs: -45.0

  # Seconds the RMS must stay above the threshold before starting.
  # activity_sustain_seconds: 3

  # Processes whose active audio use also counts as meeting activity.
  # meeting_process_names:
  #   - zoom.us
  #   - Microsoft Teams
  #   - Google Chrome
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -k auto_arm -v`
Expected: PASS (both tests).

- [ ] **Step 8: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_config.py
git commit -m "feat(config): AutoArmConfig section (off by default)"
```

---

## Task 2: `CalendarEventRepository.current_join_link_event`

**Files:**

- Modify: `src/calendar_events/repository.py` (add a read method to `CalendarEventRepository`)
- Test: `tests/test_calendar_event_repository.py`

**Interfaces:**

- Consumes: existing `CalendarEventRepository(db)`, `self._db.conn`, `self._row_to_dict`.
- Produces: `async def current_join_link_event(self, now: float, lead_seconds: float) -> dict | None` — the single earliest join-link event whose window `[start_ts - lead_seconds, end_ts]` contains `now`, or `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_calendar_event_repository.py` (the `_ev` helper and `cal_repo` fixture already exist in the file):

```python
@pytest.mark.asyncio
async def test_current_join_link_event_in_window(cal_repo):
    # Event runs 1000..2800; lead 120s. now=950 is inside [880, 2800].
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    ev = await cal_repo.current_join_link_event(now=950.0, lead_seconds=120.0)
    assert ev is not None
    assert ev["event_uid"] == "EK1:1000"
    assert ev["end_ts"] == 2800.0


@pytest.mark.asyncio
async def test_current_join_link_event_none_before_lead_window(cal_repo):
    # now=800 is before start-lead (880) — not yet armed.
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    ev = await cal_repo.current_join_link_event(now=800.0, lead_seconds=120.0)
    assert ev is None


@pytest.mark.asyncio
async def test_current_join_link_event_none_after_end(cal_repo):
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    ev = await cal_repo.current_join_link_event(now=3000.0, lead_seconds=120.0)
    assert ev is None


@pytest.mark.asyncio
async def test_current_join_link_event_skips_events_without_join_url(cal_repo):
    no_link = CalendarEvent(
        event_uid="EK2:1000",
        title="In person",
        start_ts=1000.0,
        end_ts=2800.0,
        attendees=[{"name": "A", "email": "a@x.com"}],
        organizer=None,
        join_url="",  # no virtual link
        meeting_id="",
        calendar_name="Work",
    )
    await cal_repo.upsert(no_link)
    ev = await cal_repo.current_join_link_event(now=1500.0, lead_seconds=120.0)
    assert ev is None


@pytest.mark.asyncio
async def test_current_join_link_event_picks_earliest_on_overlap(cal_repo):
    await cal_repo.upsert(_ev(uid="EK_LATE:1500", start=1500.0))
    await cal_repo.upsert(_ev(uid="EK_EARLY:1000", start=1000.0))
    ev = await cal_repo.current_join_link_event(now=1600.0, lead_seconds=120.0)
    assert ev["event_uid"] == "EK_EARLY:1000"  # ORDER BY start_ts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calendar_event_repository.py -k current_join_link -v`
Expected: FAIL — `AttributeError: ... has no attribute 'current_join_link_event'`.

- [ ] **Step 3: Add the read method**

In `src/calendar_events/repository.py`, add this method to `CalendarEventRepository` (place it after `list_by_range`, keeping reads together — a read method takes **no** `write_lock` and does **not** commit):

```python
    async def current_join_link_event(
        self, now: float, lead_seconds: float
    ) -> dict | None:
        """Return the earliest join-link event whose armed window contains ``now``.

        Armed window is ``[start_ts - lead_seconds, end_ts]``. Only events with
        a non-empty ``join_url`` (virtual meetings) qualify. Deterministic on
        overlap via ``ORDER BY start_ts``. Read-only: no write_lock, no commit.
        """
        cur = await self._db.conn.execute(
            "SELECT * FROM calendar_events "
            "WHERE join_url != '' AND (start_ts - ?) <= ? AND end_ts >= ? "
            "ORDER BY start_ts LIMIT 1",
            (lead_seconds, now, now),
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_calendar_event_repository.py -v`
Expected: PASS (existing tests + 5 new).

- [ ] **Step 5: Commit**

```bash
git add src/calendar_events/repository.py tests/test_calendar_event_repository.py
git commit -m "feat(calendar): current_join_link_event lookup for auto-arm"
```

---

## Task 3: `AudioMonitor` (BlackHole-only RMS watcher)

**Files:**

- Create: `src/audio_monitor.py`
- Test: `tests/test_audio_monitor.py`

**Interfaces:**

- Produces:
  - `class AudioMonitor(*, blackhole_device_name: str, sample_rate: int, threshold_dbfs: float = -45.0, sustain_seconds: float = 3.0, clock=time.monotonic)`
  - `observe(self, rms: float, now: float) -> None` — pure sustain-state update (feeds off linear RMS).
  - `active(self) -> bool` — True once RMS has stayed ≥ `threshold_dbfs` for `sustain_seconds`.
  - `start(self) -> None` — opens the BlackHole `sd.InputStream` (guarded; on failure `active()` stays `False`).
  - `stop(self) -> None` — closes the stream and resets sustain state.

- [ ] **Step 1: Write the failing test**

Create `tests/test_audio_monitor.py`:

```python
"""Tests for the BlackHole-only auto-arm audio monitor (pure sustain core)."""

from src.audio_monitor import AudioMonitor


def _monitor():
    # threshold -45 dBFS, sustain 3s. Stream is never opened in these tests.
    return AudioMonitor(
        blackhole_device_name="BlackHole 2ch",
        sample_rate=16000,
        threshold_dbfs=-45.0,
        sustain_seconds=3.0,
    )


def test_inactive_before_any_sample():
    assert _monitor().active() is False


def test_activates_after_sustained_loud_audio():
    m = _monitor()
    # rms 0.1 -> -20 dBFS, well above -45.
    m.observe(0.1, now=0.0)
    assert m.active() is False  # 0s elapsed
    m.observe(0.1, now=2.0)
    assert m.active() is False  # 2s < 3s sustain
    m.observe(0.1, now=3.0)
    assert m.active() is True  # 3s >= sustain


def test_quiet_audio_never_activates():
    m = _monitor()
    # rms 1e-4 -> -80 dBFS, below -45.
    for t in (0.0, 3.0, 6.0):
        m.observe(1e-4, now=t)
    assert m.active() is False


def test_dropping_below_threshold_resets_sustain():
    m = _monitor()
    m.observe(0.1, now=0.0)
    m.observe(0.1, now=3.0)
    assert m.active() is True
    m.observe(1e-4, now=3.5)  # silence
    assert m.active() is False
    m.observe(0.1, now=4.0)  # loud again but clock restarts
    assert m.active() is False
    m.observe(0.1, now=7.0)  # 3s after the restart
    assert m.active() is True


def test_silence_floor_does_not_crash_on_zero_rms():
    m = _monitor()
    m.observe(0.0, now=0.0)  # rms 0 -> -100 dBFS floor
    assert m.active() is False


def test_stop_resets_state():
    m = _monitor()
    m.observe(0.1, now=0.0)
    m.observe(0.1, now=3.0)
    assert m.active() is True
    m.stop()
    assert m.active() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_audio_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.audio_monitor'`.

- [ ] **Step 3: Write the implementation**

Create `src/audio_monitor.py`:

```python
"""BlackHole-only system-audio RMS monitor for calendar auto-arm.

While an event is armed (but nothing is recording yet), this opens a
single BlackHole input stream and watches system-audio level. ``active()``
returns True once the RMS stays above ``threshold_dbfs`` for
``sustain_seconds`` — the "a meeting actually started" signal. No files are
written; the stream is mutually exclusive with the real capture (both read
BlackHole), so the controller closes it before recording begins.

The sustain logic (``observe``/``active``) is a pure, time-injected state
machine tested without any real audio. Stream I/O in ``start``/``stop`` is
guarded: if the device can't be opened, ``active()`` simply stays False and
auto-arm falls back to the process signal.
"""

import logging
import math
import time

import numpy as np
import sounddevice as sd

from src.audio_devices import refresh_input_devices

logger = logging.getLogger(__name__)


class AudioMonitor:
    def __init__(
        self,
        *,
        blackhole_device_name: str,
        sample_rate: int,
        threshold_dbfs: float = -45.0,
        sustain_seconds: float = 3.0,
        clock=time.monotonic,
    ) -> None:
        self._device_name = blackhole_device_name
        self._sample_rate = sample_rate
        self._threshold_dbfs = threshold_dbfs
        self._sustain_seconds = sustain_seconds
        self._clock = clock

        self._stream = None
        self._above_since: float | None = None
        self._active: bool = False

    # ------------------------------------------------------------------
    # Pure sustain core (unit-tested)
    # ------------------------------------------------------------------

    def observe(self, rms: float, now: float) -> None:
        """Record one linear-RMS sample and update the sustain latch."""
        dbfs = -100.0 if rms < 1e-10 else 20.0 * math.log10(rms)
        if dbfs >= self._threshold_dbfs:
            if self._above_since is None:
                self._above_since = now
            self._active = (now - self._above_since) >= self._sustain_seconds
        else:
            self._above_since = None
            self._active = False

    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # Stream I/O (guarded; not exercised in unit tests)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the BlackHole input stream (best-effort)."""
        if self._stream is not None:
            return
        try:
            # Un-freeze PortAudio's device table (safe: no stream open yet).
            refresh_input_devices()
            device_idx = self._find_blackhole_index()

            def _callback(indata, frames, time_info, status):
                if status:
                    logger.warning("Auto-arm monitor audio status: %s", status)
                mono = np.mean(indata, axis=1) if indata.ndim > 1 else indata
                rms = float(np.sqrt(np.mean(mono**2)))
                self.observe(rms, self._clock())

            self._stream = sd.InputStream(
                device=device_idx,
                samplerate=self._sample_rate,
                channels=2,  # BlackHole always provides stereo.
                dtype="float32",
                callback=_callback,
                blocksize=1024,
            )
            self._stream.start()
            logger.info("Auto-arm audio monitor opened on '%s'.", self._device_name)
        except Exception:
            logger.warning(
                "Auto-arm audio monitor failed to open — relying on the "
                "process signal only.",
                exc_info=True,
            )
            self._stream = None
            self._reset()

    def stop(self) -> None:
        """Close the stream and reset sustain state."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._reset()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self._above_since = None
        self._active = False

    def _find_blackhole_index(self) -> int:
        """Substring match over input devices (like AudioCapture._find_device)."""
        devices = sd.query_devices()
        name = self._device_name.lower()
        for idx, device in enumerate(devices):
            if name in device["name"].lower() and device["max_input_channels"] > 0:
                return idx
        raise RuntimeError(f"Auto-arm monitor device '{self._device_name}' not found")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_audio_monitor.py -v`
Expected: PASS (all 6).

- [ ] **Step 5: Commit**

```bash
git add src/audio_monitor.py tests/test_audio_monitor.py
git commit -m "feat(audio): BlackHole-only AudioMonitor for auto-arm"
```

---

## Task 4: `AutoArmController`

**Files:**

- Create: `src/auto_arm.py`
- Test: `tests/test_auto_arm.py`

**Interfaces:**

- Consumes:
  - `AutoArmConfig` (Task 1) — reads `lead_minutes`, `trailing_minutes`.
  - `calendar_source(now: float, lead_seconds: float) -> dict | None` — Task 6 provides the DB bridge; returns an event dict with at least `end_ts`.
  - `audio_monitor` — object with `start()`, `stop()`, `active() -> bool` (Task 3).
  - `process_active() -> bool`, `is_recording() -> bool`, `start(event: dict) -> None`, `stop() -> None`, `clock() -> float`.
- Produces: `class AutoArmController(*, config, calendar_source, audio_monitor, process_active, is_recording, start, stop, clock=time.time)` with `tick(self, now: float | None = None) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_arm.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_auto_arm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.auto_arm'`.

- [ ] **Step 3: Write the implementation**

Create `src/auto_arm.py`:

```python
"""Calendar-driven auto-arm controller.

Ticked from the detector poll loop. While a join-link calendar event is
within its armed window and nothing is recording, it opens an AudioMonitor
and watches for real meeting activity (system-audio RMS OR a meeting-app
process using audio). On activity it starts the orchestrator's recording;
it stops the recording it started once the clock passes end_ts + trailing.

All collaborators are injected so this is pure and unit-tested with fakes.
tick() never raises — the poll loop must survive any auto-arm failure.
"""

import logging
import time

logger = logging.getLogger(__name__)


class AutoArmController:
    def __init__(
        self,
        *,
        config,
        calendar_source,
        audio_monitor,
        process_active,
        is_recording,
        start,
        stop,
        clock=time.time,
    ) -> None:
        self._config = config
        self._calendar_source = calendar_source
        self._audio_monitor = audio_monitor
        self._process_active = process_active
        self._is_recording = is_recording
        self._start = start
        self._stop = stop
        self._clock = clock

        self._lead_seconds = config.lead_minutes * 60
        self._trailing_seconds = config.trailing_minutes * 60

        self._armed: bool = False  # monitor open
        self._recording_event: dict | None = None  # a recording we started

    def tick(self, now: float | None = None) -> None:
        """One poll cycle. Never raises (poll-loop resilience)."""
        try:
            self._tick(now if now is not None else self._clock())
        except Exception:
            logger.exception("Auto-arm tick failed — ignoring.")

    def _tick(self, now: float) -> None:
        recording = self._is_recording()

        # 1. Manage a recording we own.
        if self._recording_event is not None:
            if not recording:
                # Ended by other means (Teams-end / manual / silence watchdog).
                self._recording_event = None
            else:
                end_ts = self._recording_event.get("end_ts", 0.0)
                if now > end_ts + self._trailing_seconds:
                    self._recording_event = None
                    self._stop()
            return

        # 2. Someone else is recording — stay out of the way.
        if recording:
            self._disarm()
            return

        # 3. Idle: is a join-link event armed right now?
        event = self._calendar_source(now, self._lead_seconds)
        if event is None:
            self._disarm()
            return

        # 4. Armed — open the monitor and watch for activity.
        self._arm()
        if self._audio_monitor.active() or self._process_active():
            self._disarm()  # close before the real capture takes BlackHole
            self._recording_event = event
            self._start(event)

    def _arm(self) -> None:
        if not self._armed:
            self._audio_monitor.start()
            self._armed = True

    def _disarm(self) -> None:
        if self._armed:
            self._audio_monitor.stop()
            self._armed = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_auto_arm.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/auto_arm.py tests/test_auto_arm.py
git commit -m "feat(auto-arm): AutoArmController orchestration logic"
```

---

## Task 5: Detector `on_tick` hook + `app_using_audio` passthrough

**Files:**

- Modify: `src/detector.py` (`TeamsDetector.__init__` ~line 82; `_tick` ~line 118; add a public method)
- Test: `tests/test_detector.py`

**Interfaces:**

- Produces:
  - `TeamsDetector.on_tick: Callable[[], None]` — a settable attribute fired once per poll, defensively wrapped so a raising hook can't stop detection.
  - `TeamsDetector.app_using_audio(self, process_names: list[str]) -> bool` — public passthrough to the platform detector (so the orchestrator's process signal doesn't reach into `_platform`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_detector.py` (this file already constructs `TeamsDetector` with a fake platform — match its existing fixture/fake style; the fake below is self-contained):

```python
def test_on_tick_hook_fires_each_poll():
    from unittest.mock import Mock
    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Idle:
        def is_app_running(self, names):
            return False

        def is_app_using_audio(self, names):
            return False

        def is_call_window_active(self):
            return False

    detector = TeamsDetector(DetectionConfig(), platform=_Idle())
    hook = Mock()
    detector.on_tick = hook

    detector._tick()
    detector._tick()

    assert hook.call_count == 2


def test_on_tick_exception_does_not_break_tick():
    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Idle:
        def is_app_running(self, names):
            return False

        def is_app_using_audio(self, names):
            return False

        def is_call_window_active(self):
            return False

    detector = TeamsDetector(DetectionConfig(), platform=_Idle())
    detector.on_tick = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    detector._tick()  # must not raise


def test_app_using_audio_delegates_to_platform():
    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Fake:
        def __init__(self):
            self.seen = None

        def is_app_running(self, names):
            return True

        def is_app_using_audio(self, names):
            self.seen = names
            return True

        def is_call_window_active(self):
            return False

    fake = _Fake()
    detector = TeamsDetector(DetectionConfig(), platform=fake)
    assert detector.app_using_audio(["zoom.us"]) is True
    assert fake.seen == ["zoom.us"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_detector.py -k "on_tick or app_using_audio" -v`
Expected: FAIL — `on_tick` not fired / `app_using_audio` missing.

- [ ] **Step 3: Add the hook attribute**

In `src/detector.py`, in `TeamsDetector.__init__`, directly after the existing callback assignments (~lines 82-83):

```python
        self.on_meeting_start: Callable[[MeetingEvent], None] = lambda event: None
        self.on_meeting_end: Callable[[MeetingEvent], None] = lambda event: None
        # Fired once per poll (auto-arm hooks here). Kept separate from the
        # edge-triggered start/end callbacks and wrapped so a raising hook
        # can never stop detection.
        self.on_tick: Callable[[], None] = lambda: None
```

- [ ] **Step 4: Fire the hook at the top of `_tick` + add the passthrough**

In `src/detector.py`, change the start of `_tick` (line 118) from:

```python
    def _tick(self) -> None:
        """Single poll cycle. Advances the state machine with debounce."""
        meeting_active = self._is_meeting_active()
```

to:

```python
    def _tick(self) -> None:
        """Single poll cycle. Advances the state machine with debounce."""
        self._fire_tick_hook()
        meeting_active = self._is_meeting_active()
```

Then add these two methods to the class (e.g. directly after `_tick`, before the `run` section):

```python
    def _fire_tick_hook(self) -> None:
        """Invoke the per-poll hook, isolating detection from hook failures."""
        try:
            self.on_tick()
        except Exception:
            logger.exception("on_tick hook raised — ignoring.")

    def app_using_audio(self, process_names: list[str]) -> bool:
        """Public passthrough to the platform's active-audio check."""
        return self._platform.is_app_using_audio(process_names)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_detector.py -v`
Expected: PASS (existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/detector.py tests/test_detector.py
git commit -m "feat(detector): per-poll on_tick hook + app_using_audio passthrough"
```

---

## Task 6: Orchestrator wiring

**Files:**

- Modify: `src/main.py` (`ContextRecall.__init__` ~line 126 for state; new helpers; `run_daemon` ~line 982 after `_start_api_server()`)
- Test: `tests/test_orchestrator.py`

**Interfaces:**

- Consumes: `AutoArmController` (Task 4), `AudioMonitor` (Task 3), `CalendarEventRepository.current_join_link_event` (Task 2), `TeamsDetector.on_tick` + `app_using_audio` (Task 5), `AutoArmConfig` (Task 1).
- Produces:
  - `ContextRecall._maybe_start_auto_arm(self) -> None` — constructs + wires the controller when enabled; sets `self._auto_arm` and `self._detector.on_tick`.
  - `ContextRecall._calendar_source(self, now, lead_seconds) -> dict | None` — bounded DB bridge.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py` (uses the existing `tmp_config` fixture; add a helper that writes auto-arm config):

```python
def _auto_arm_config(tmp_path, *, enabled, import_enabled=True):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    config = {
        "audio": {"temp_audio_dir": str(tmp_path / "audio")},
        "api": {"enabled": False},
        "diarisation": {"enabled": False},
        "markdown": {"enabled": False},
        "notion": {"enabled": False},
        "calendar": {"import_enabled": import_enabled},
        "auto_arm": {"enabled": enabled},
        "logging": {"level": "WARNING", "log_file": str(log_dir / "t.log")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


def test_auto_arm_wired_when_enabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=True))
    app._maybe_start_auto_arm()

    assert app._auto_arm is not None
    assert app._detector.on_tick == app._auto_arm.tick


def test_auto_arm_absent_when_disabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(config_path=_auto_arm_config(tmp_path, enabled=False))
    default_hook = app._detector.on_tick
    app._maybe_start_auto_arm()

    assert app._auto_arm is None
    assert app._detector.on_tick is default_hook  # unchanged


def test_auto_arm_absent_when_calendar_import_disabled(tmp_path):
    from src.main import ContextRecall

    app = ContextRecall(
        config_path=_auto_arm_config(tmp_path, enabled=True, import_enabled=False)
    )
    app._maybe_start_auto_arm()

    assert app._auto_arm is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_orchestrator.py -k auto_arm -v`
Expected: FAIL — `AttributeError: 'ContextRecall' object has no attribute '_maybe_start_auto_arm'`.

- [ ] **Step 3: Add the `_auto_arm` state field**

In `src/main.py`, in `ContextRecall.__init__`, directly after `self._api_server = None` / `self._event_bus = None` (~lines 126-127), add:

```python
        # Calendar auto-arm controller (constructed at daemon boot when enabled).
        self._auto_arm = None
```

- [ ] **Step 4: Add the imports**

At the top of `src/main.py`, alongside the other `from src.*` imports, add:

```python
from src.audio_monitor import AudioMonitor
from src.auto_arm import AutoArmController
```

- [ ] **Step 5: Add the wiring helpers**

In `src/main.py`, add these methods to `ContextRecall` (place them near the run-mode helpers, e.g. after `_start_api_server`):

```python
    def _calendar_source(self, now: float, lead_seconds: float) -> dict | None:
        """Resolve the currently-armed join-link event via the API loop.

        Bounded and best-effort: the controller only calls this while NOT
        recording (the API loop is idle then), so the short blocking wait
        can't stack behind a running pipeline.
        """
        import asyncio

        from src.calendar_events.repository import CalendarEventRepository

        server = self._api_server
        if server is None:
            return None
        loop = server.loop
        if loop is None or loop.is_closed():
            return None
        try:
            repo = CalendarEventRepository(server.db)
            future = asyncio.run_coroutine_threadsafe(
                repo.current_join_link_event(now, lead_seconds), loop
            )
            return future.result(timeout=2.0)
        except Exception:
            logger.debug("Auto-arm calendar lookup failed", exc_info=True)
            return None

    def _auto_arm_process_active(self) -> bool:
        """Meeting-app process signal for auto-arm (best-effort)."""
        try:
            return self._detector.app_using_audio(
                self._config.auto_arm.meeting_process_names
            )
        except Exception:
            logger.debug("Auto-arm process check failed", exc_info=True)
            return False

    def _auto_arm_start(self, event: dict) -> None:
        """Start recording for an armed event (mic-gate failures are no-ops)."""
        try:
            logger.info("Auto-arm: activity detected — starting recording.")
            self.api_start_recording()
        except AudioCaptureError as exc:
            logger.warning("Auto-arm start blocked: %s", exc)

    def _auto_arm_stop(self) -> None:
        """Stop an auto-armed recording without blocking the poll thread.

        api_stop_recording() blocks up to 30s on the post-merge; run it on a
        throwaway daemon thread so detection keeps polling.
        """
        threading.Thread(
            target=self.api_stop_recording,
            name="auto-arm-stop",
            daemon=True,
        ).start()

    def _maybe_start_auto_arm(self) -> None:
        """Construct + wire the auto-arm controller when opted in."""
        if not (
            self._config.auto_arm.enabled and self._config.calendar.import_enabled
        ):
            return
        monitor = AudioMonitor(
            blackhole_device_name=self._config.audio.blackhole_device_name,
            sample_rate=self._config.audio.sample_rate,
            threshold_dbfs=self._config.auto_arm.activity_rms_dbfs,
            sustain_seconds=self._config.auto_arm.activity_sustain_seconds,
        )
        self._auto_arm = AutoArmController(
            config=self._config.auto_arm,
            calendar_source=self._calendar_source,
            audio_monitor=monitor,
            process_active=self._auto_arm_process_active,
            is_recording=lambda: self._capture.is_recording,
            start=self._auto_arm_start,
            stop=self._auto_arm_stop,
            clock=time.time,
        )
        self._detector.on_tick = self._auto_arm.tick
        logger.info("Calendar auto-arm enabled.")
```

- [ ] **Step 6: Call the wiring from `run_daemon`**

In `src/main.py`, in `run_daemon`, directly after `self._start_api_server()` (~line 982), add:

```python
        # Wire calendar auto-arm (opt-in) now that the API loop exists.
        self._maybe_start_auto_arm()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_orchestrator.py -k auto_arm -v`
Expected: PASS (all 3).

- [ ] **Step 8: Run the full touched-module suites + lint**

Run: `python3 -m pytest tests/test_orchestrator.py tests/test_auto_arm.py tests/test_audio_monitor.py tests/test_detector.py tests/test_calendar_event_repository.py tests/test_config.py -q && ruff check src/ tests/`
Expected: PASS + clean.

- [ ] **Step 9: Commit**

```bash
git add src/main.py tests/test_orchestrator.py
git commit -m "feat(auto-arm): wire AutoArmController into the daemon poll loop"
```

---

## Task 7: UI — Auto-record Settings toggle

**Files:**

- Modify: `ui/src/lib/types.ts` (add `AutoArmConfig` ~near line 257; add `auto_arm` to `AppConfig` ~line 309)
- Create: `ui/src/components/settings/AutoArmSection.tsx`
- Modify: `ui/src/components/settings/Settings.tsx` (`SETTINGS_SECTIONS` ~line 31; render near the other self-managed sections ~line 2045)
- Test: `ui/src/components/settings/__tests__/AutoArmSection.test.tsx`

**Interfaces:**

- Consumes: `getConfig`/`updateConfig` from `../../lib/api` (config GET / `DeepPartial<AppConfig>` PUT), `useToast`.
- Produces: `AutoArmConfig` TS interface; `auto_arm: AutoArmConfig` on `AppConfig`; `AutoArmSection({ id })` component.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/settings/__tests__/AutoArmSection.test.tsx` (models the `CalendarsSection.test.tsx` fetch-mock + PUT-assertion pattern):

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { AutoArmSection } from "../AutoArmSection";
import { ToastProvider } from "../../common/Toast";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

const CONFIG = {
  auto_arm: {
    enabled: false,
    lead_minutes: 2,
    trailing_minutes: 5,
    activity_rms_dbfs: -45,
    activity_sustain_seconds: 3,
    meeting_process_names: ["zoom.us"],
  },
};

describe("AutoArmSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      if (url.includes("/api/config") && init?.method === "PUT") {
        return new Response(JSON.stringify(CONFIG), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      // GET /api/config
      return new Response(JSON.stringify(CONFIG), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("renders the toggle off from config", async () => {
    render(<AutoArmSection id="auto_arm" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Auto-record scheduled meetings" }),
      ).toHaveAttribute("aria-checked", "false"),
    );
  });

  it("PUTs auto_arm.enabled=true when toggled on", async () => {
    render(<AutoArmSection id="auto_arm" />, { wrapper: makeWrapper() });
    const sw = await screen.findByRole("switch", {
      name: "Auto-record scheduled meetings",
    });

    fireEvent.click(sw);

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === "PUT")!;
    const body = JSON.parse(init?.body as string);
    expect(body.auto_arm.enabled).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- AutoArmSection`
Expected: FAIL — cannot resolve `../AutoArmSection`.

- [ ] **Step 3: Add the TS types**

In `ui/src/lib/types.ts`, add an interface near `CalendarConfig` (~line 257):

```typescript
export interface AutoArmConfig {
  enabled: boolean;
  lead_minutes: number;
  trailing_minutes: number;
  activity_rms_dbfs: number;
  activity_sustain_seconds: number;
  meeting_process_names: string[];
}
```

Then add the field to the `AppConfig` interface, directly after `calendar: CalendarConfig;` (~line 309):

```typescript
auto_arm: AutoArmConfig;
```

- [ ] **Step 4: Create the section component**

Create `ui/src/components/settings/AutoArmSection.tsx`:

```tsx
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getConfig, updateConfig } from "../../lib/api";
import { useToast } from "../common/Toast";

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-accent" : "bg-border"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-[2px]"
        }`}
      />
    </button>
  );
}

/** Settings panel: master switch for calendar auto-arm recording. */
export function AutoArmSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  const enabled = config?.auto_arm?.enabled ?? false;

  const save = useMutation({
    mutationFn: (next: boolean) =>
      updateConfig({ auto_arm: { enabled: next } }),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Auto-record setting saved.");
    },
    onError: () => toast.error("Failed to save auto-record setting."),
  });

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Auto-record</legend>
      <h2 className="text-sm font-medium text-text-primary">Auto-record</h2>
      <p className="text-xs text-text-muted mt-1">
        Automatically start recording scheduled meetings that have a join link.
        Requires calendar import.
      </p>

      <div className="py-3 flex items-center justify-between">
        <span className="text-sm text-text-secondary">
          Auto-record scheduled meetings
        </span>
        <Toggle
          checked={enabled}
          onChange={(v) => save.mutate(v)}
          label="Auto-record scheduled meetings"
        />
      </div>
    </fieldset>
  );
}
```

- [ ] **Step 5: Render it in Settings + add the nav entry**

In `ui/src/components/settings/Settings.tsx`:

(a) Add to the `SETTINGS_SECTIONS` array (~line 31-51), after the `{ id: "calendars", label: "Calendars" }` entry:

```typescript
  { id: "auto_arm", label: "Auto-record" },
```

(b) Add the import near the other section imports (~line 23-25):

```typescript
import { AutoArmSection } from "./AutoArmSection";
```

(c) Render it alongside the other daemon-gated self-managed sections, directly after the `{daemonRunning && <CalendarsSection id="calendars" />}` line (~line 2045):

```tsx
{
  daemonRunning && <AutoArmSection id="auto_arm" />;
}
```

- [ ] **Step 6: Run the test + type check**

Run: `cd ui && npm test -- AutoArmSection && npx tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 7: Commit**

```bash
git add ui/src/lib/types.ts ui/src/components/settings/AutoArmSection.tsx ui/src/components/settings/Settings.tsx ui/src/components/settings/__tests__/AutoArmSection.test.tsx
git commit -m "feat(ui): Auto-record scheduled meetings settings toggle"
```

---

## Final Validation

- [ ] **Full Python touched-suite + lint:**

```bash
python3 -m pytest tests/test_config.py tests/test_calendar_event_repository.py tests/test_audio_monitor.py tests/test_auto_arm.py tests/test_detector.py tests/test_orchestrator.py -q
ruff check src/ tests/
```

- [ ] **UI suite + type check:**

```bash
cd ui && npm test && npx tsc --noEmit
```

---

## Self-Review Notes (author)

- **Spec coverage:** arm-during-window + activity-start (Task 4); audio RMS signal (Task 3) OR process signal (Tasks 4/6); join-link-only arm source (Task 2); orchestrator-hosted, poll-loop-ticked (Tasks 5/6); own meeting-app list (Tasks 1/6); off-by-default + import-gated (Tasks 1/6); calendar-driven stop at end+trailing (Task 4); Settings toggle (Task 7); graceful degradation — monitor open failure → process-only (Task 3), no server/closed loop → `None` (Task 6), tick never crashes the poll loop (Tasks 4/5). All §6 error rows map to Task 4 branches or Task 3/6 guards.
- **Deviation from spec §4 sketch (intentional):** the "which recording is auto-armed" marker lives in the controller as `self._recording_event` (source of truth for the stop decision), not as an orchestrator `self._auto_armed_event`. This keeps the stop logic inside the unit-tested controller and means a Teams/manual/silence stop is handled by the `not recording` release branch. Equivalent behaviour, better tested.
- **Deviation from spec §8 wording (intentional):** the toggle is a small self-managed `AutoArmSection` (config GET/PUT), modelled on the existing, already-tested `CalendarsSection`, rather than a field inside the monolithic main `<Settings>` form (which has no test harness and would need a fragile full-screen render). It still "reuses the config GET/PUT + form pattern" and adds `AutoArmConfig` to the TS `AppConfig`, exactly as the spec requires.
- **Type consistency:** `calendar_source(now, lead_seconds)`, `audio_monitor.{start,stop,active}`, `process_active()`, `is_recording()`, `start(event)`, `stop()`, `clock()` are used identically in Tasks 3/4/6. `current_join_link_event(now, lead_seconds) -> dict | None` matches its Task-6 caller. `on_tick` / `app_using_audio` (Task 5) match their Task-6 consumers.
- **No placeholders:** every code + test step is complete.
