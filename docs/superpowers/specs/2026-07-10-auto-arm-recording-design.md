# Auto-Arm Recording — Design (Track B, Phase 4)

**Date:** 2026-07-10
**Track:** B — "Calendar hub", Phase 4 (auto-arm / auto-start recording for scheduled meetings)
**Depends on:** phases 1–3 (calendar import + auto-prep + popover actions), **all merged to `main`**. Branches off `main`.

## 1. Problem & context

Recording starts today via three paths: the `TeamsDetector` poll loop (Teams process **using audio** + window-title fallback, with debounce → `on_meeting_start`), a manual `POST /api/record/start`, and `--record-now`. All funnel through the orchestrator's single `AudioCapture` (`is_recording` guards a single active recording), and `CalendarMatcher` links an in-progress recording to a calendar event by time-window.

Gap: for a **scheduled** meeting that isn't a detected Teams call (Zoom, Meet, or a missed detection), nothing auto-records. Phase 4 adds a **calendar-driven auto-arm**: during a scheduled join-link meeting's window the daemon _arms_, watches for real meeting activity (system audio **or** a meeting-app process), and starts recording when activity appears — then stops at the event's end. It never records an event you don't actually join (activity-gated), and never stacks on an existing recording (the `is_recording` guard).

## 2. Goals / non-goals

**Goals:**

- A calendar-armed activity watcher, in the orchestrator, that auto-starts recording for **join-link** scheduled events on audio-or-process activity, and stops at the event end.
- Opt-in (off by default — it records without an explicit user action), gated additionally on the existing mic grant + calendar import.
- Reuse the existing single-recording capture + `CalendarMatcher` linkage; never double-trigger.
- A minimal Settings toggle for the master switch.
- Degrade gracefully (no BlackHole, no calendar data, disabled) without crashing the poll loop.

**Non-goals:** the dashboard "next up" widget (final phase); changing the base `TeamsDetector` behaviour; RSVP/organizer-based filtering (EventKit RSVP isn't captured); a full recording UI.

## 3. Decisions (from brainstorming)

| Decision        | Choice                                                                                                                                                |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Trigger model   | **Arm during the event window, start on activity** (not a hard start-at-time).                                                                        |
| Activity signal | **Either** system-audio RMS **or** a meeting-app process using audio — whichever fires first.                                                         |
| Which events    | **Only events with a join link** (virtual meetings; the app's system-audio capture is meaningful there).                                              |
| Where it lives  | The **orchestrator** (`src/main.py`), ticked from the existing detection poll loop — not the API-server scheduler.                                    |
| Process signal  | Auto-arm keeps its **own** meeting-app list (reusing `PlatformDetector.is_app_using_audio`), only while armed. The base `TeamsDetector` is unchanged. |
| Arm source      | The foundation's **`calendar_events` mirror** (join-link rows), queried via the API loop. Requires `calendar.import_enabled`.                         |
| Opt-in          | Off by default (`auto_arm.enabled = false`); also gated on the mic TCC grant.                                                                         |
| Stop            | Calendar-driven at `end_ts + trailing`; the existing silence-watchdog / manual / Teams-end stop can end it sooner.                                    |

## 4. Architecture

Auto-arm runs **in the orchestrator**, co-located with the audio layer + `TeamsDetector`, reusing the detection poll loop.

```
Orchestrator (ContextRecall, src/main.py) — owns _capture, _detector, start/stop, is_recording
   │
   ├─ TeamsDetector (existing, UNCHANGED) — named-app-using-audio start path.
   │
   ├─ AutoArmController (NEW, src/auto_arm.py) — .tick(now) called each poll iteration:
   │     1. armed event? = calendar_events join-link row with (start_ts − lead) ≤ now ≤ end_ts
   │     2. arm/disarm the AudioMonitor accordingly (open only while armed AND not is_recording)
   │     3. while armed && !is_recording, if activity (monitor.active() OR process-poll) → start()
   │     4. if an auto-armed recording is running and now > end_ts + trailing → stop()
   │     — all collaborators injected (calendar source, audio monitor, process check, clock, start, stop, is_recording)
   │
   ├─ AudioMonitor (NEW, src/audio_monitor.py) — BlackHole-only sd.InputStream, computes system RMS,
   │     active() = RMS sustained above threshold for sustain_seconds; NO files; open only while armed & !recording.
   │
   └─ start/stop funnel through the orchestrator (is_recording guard dedupes; CalendarMatcher links by time-window).
        self._auto_armed_event marks an auto-armed recording so only it gets the calendar stop.
```

**Data flow.**

- _Arm:_ each poll tick asks `current_join_link_event(now, lead)`; if present → arm (open the monitor if not recording); else → disarm (close it).
- _Start:_ while armed & `!is_recording`, `AudioMonitor.active()` **or** `PlatformDetector.is_app_using_audio(auto_arm.meeting_process_names)` → orchestrator start path (full pipeline + calendar match), guarded by `is_recording`. The recording is tagged `self._auto_armed_event = event`.
- _Stop:_ when `self._auto_armed_event` is set, the recording is active, and `now > event.end_ts + trailing` → orchestrator stop. Manual/silence/Teams-end stop still works and clears the tag. The monitor closes whenever recording starts (mutually exclusive with capture over BlackHole).

## 5. Components & config

**`src/auto_arm.py` — `AutoArmController`** (unit-testable; no real audio/EventKit/DB):

```python
class AutoArmController:
    def __init__(self, *, config: AutoArmConfig, calendar_source, audio_monitor,
                 process_active, is_recording, start, stop, clock=time.time): ...
    def tick(self, now: float | None = None) -> None:
        # resolve armed event; arm/disarm monitor; start on activity; stop past end+trailing.
```

- `calendar_source(now, lead_seconds) -> dict | None` — the current join-link event.
- `audio_monitor` — `.start()`, `.stop()`, `.active() -> bool`.
- `process_active() -> bool` — `PlatformDetector.is_app_using_audio(meeting_process_names)`.
- `is_recording() -> bool`, `start(event) -> None`, `stop() -> None`.
  `tick` is wrapped so exceptions are logged, not raised (poll-loop resilience).

**`src/audio_monitor.py` — `AudioMonitor`**: `start()` opens a BlackHole-only `sd.InputStream` (device via `audio_devices` resolution), the callback feeds an RMS helper; `active()` returns true once RMS ≥ `activity_rms_dbfs` sustained for `activity_sustain_seconds`; `stop()` closes the stream. Guarded/import-safe; on open failure it logs and reports `active()` = `False` forever (auto-arm then relies on the process signal).

**`CalendarEventRepository.current_join_link_event(now, lead_seconds) -> dict | None`** — `SELECT * FROM calendar_events WHERE join_url != '' AND start_ts - ? <= ? AND end_ts >= ? ORDER BY start_ts LIMIT 1` (deterministic on overlap).

**Config — new `AutoArmConfig`:**

```python
enabled: bool = False
lead_minutes: int = 2
trailing_minutes: int = 5
activity_rms_dbfs: float = -45.0
activity_sustain_seconds: int = 3
meeting_process_names: list[str] = field(default_factory=lambda: ["zoom.us", "Microsoft Teams", "Google Chrome"])
```

Added to `AppConfig` as `auto_arm` and to `load_config`'s explicit section list (per the config convention). The config route auto-derives, so GET/PUT pick it up.

**Orchestrator wiring (`src/main.py`):** construct the `AutoArmController` (with the monitor, a `calendar_source` that runs `CalendarEventRepository.current_join_link_event` on `self._api_server.loop` via `run_coroutine_threadsafe`, `process_active` from the platform detector, and `start`/`stop`/`is_recording` bound to the capture) only when `config.auto_arm.enabled and config.calendar.import_enabled`; call `controller.tick()` inside the existing detection poll loop. `self._auto_armed_event` is set by the injected `start` and cleared by `stop`.

## 6. Error handling

Each degrades; the poll loop is never crashed (`tick` swallows + logs).

| Condition                                          | Behaviour                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `auto_arm.enabled` off                             | controller not constructed / not ticked                                                     |
| `calendar.import_enabled` off or no join-link rows | `current_join_link_event` → `None` → never armed                                            |
| monitor fails to open (no BlackHole / PortAudio)   | logged; `active()` stays `False`; auto-arm relies on the process signal                     |
| already recording (Teams/manual)                   | armed watch skipped (`armed && !is_recording`); auto-arm won't start or stop that recording |
| no mic TCC grant                                   | existing start gate blocks + warns; auto-arm start no-ops                                   |
| event moves/ends while recording                   | stop uses `end_ts` captured at start; manual/silence stop still works, clears the tag       |
| overlapping join-link events                       | deterministic pick (`ORDER BY start_ts`)                                                    |

## 7. Testing

No real audio / EventKit / DB — all collaborators faked.

- `tests/test_auto_arm.py` — `AutoArmController` with fake clock/calendar-source/audio-monitor/process-check/start/stop/is_recording: not armed when disabled or no join-link event; armed within `[start−lead, end]`; starts on audio activity; starts on process activity; does NOT start while already recording; stops an auto-armed recording past `end_ts + trailing`; does NOT stop a non-auto-armed recording; disarm closes the monitor.
- `tests/test_audio_monitor.py` — RMS-from-frames + `active()` sustain/debounce (pure); the `sd.InputStream` I/O guarded and not opened in tests.
- extend `tests/test_calendar_event_repository.py` — `current_join_link_event` (in-window join-link row; `None` out-of-window / empty `join_url`).
- `tests/test_config.py` — `AutoArmConfig` defaults; `config.example.yaml` updated.
- orchestrator wiring test — auto-arm ticked when enabled, absent when disabled.
- UI — vitest for the Settings "Auto-record scheduled meetings" toggle (binds `auto_arm.enabled` via the config PUT).

## 8. Frontend

Minimal: a **"Auto-record scheduled meetings"** toggle in the existing Settings config form bound to `auto_arm.enabled` (reuses the config GET/PUT + form pattern; add `AutoArmConfig` to the TS `AppConfig`), with a one-line hint that it requires calendar import. No other UI.

## 9. Roadmap — remaining phase

- **Dashboard "next up" widget** — its own spec→plan→build (the final Track B item).
