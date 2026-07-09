# Calendar Import — Design (Track B, Foundation)

**Date:** 2026-07-09
**Track:** B — "Calendar hub" (macOS EventKit)
**Scope:** Foundation phase only. Later phases are listed in [§10 Roadmap](#10-roadmap--later-phases) and are explicitly planned, not dropped.

## 1. Problem & context

Every calendar interaction in Context Recall today is **reactive**: `CalendarMatcher.match(started_at)` reads EventKit only _after_ a recording has started, querying a ±15-minute window to grab the event title and attendees. Nothing reads **upcoming** events, so:

- The app never knows what meetings are scheduled ahead of time.
- The `CalendarView` UI (a full month/week/day/agenda calendar with a heatmap) can only render **recorded** meetings — every future date is empty.
- The prep-briefing generator (`src/prep/briefing.py`) is effectively **orphaned**: it can build a briefing but has no trigger to fire _before_ a meeting, because nothing knows a meeting is coming.

"Calendar import" (the user's Track B / deferred bug #6) closes this by proactively importing upcoming calendar events. The user chose the **broad hub** (agenda + auto-prep + auto-arming) but sequenced it **foundation-first, then layer**. This spec covers the foundation: **import upcoming events + store them + merge them into the existing calendar UI.**

## 2. Goals / non-goals

**Goals (this phase):**

- Read upcoming **meeting-like** events from macOS Calendar and expose them to the UI.
- Merge upcoming events into the existing `CalendarView` grids, visually distinct from recorded meetings.
- Persist a rolling near-term window of events to SQLite as the substrate later phases consume.
- A Settings control to exclude specific calendars.
- Everything degrades gracefully when EventKit is unavailable/unauthorized (no crashes, no new permission prompts beyond the existing calendar grant).

**Non-goals (this phase — see §10):** auto-generating prep briefings, auto-arming/starting recording, prep/record actions on the event popover, dashboard "next up" widget.

## 3. Decisions (from brainstorming)

| Decision     | Choice                                                                                                                                                      |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Import scope | **Meeting-like events only** — has a join link (Teams/Zoom/Meet) **or** ≥2 non-self attendees. All-day events skipped.                                      |
| Calendars    | **All calendars**, minus a user-managed exclude-list in Settings.                                                                                           |
| Agenda UX    | **Merge into the existing `CalendarView`** — upcoming events populate the same grids, visually distinguished; click → event popover (info-only this phase). |
| Storage/sync | **Hybrid** — UI reads **live** per visible range; a background job **mirrors** the rolling window into a new table as the automation foundation.            |

## 4. Architecture

```
EventKit (macOS, daemon process)
   │
   ├─ CalendarReader  (NEW — extracted from CalendarMatcher)
   │     • shared EventKit auth + store handling
   │     • list_events(start, end) → [CalendarEvent]   (range read; meeting-like + exclude filter)
   │     • list_calendars() → [{id, title}]            (for the Settings exclude UI)
   │
   ├─ CalendarMatcher (existing) — keeps match(); delegates EventKit access to CalendarReader
   │
   ├─ CalendarSyncJob (NEW — registered in ApiServer scheduler; boot + every sync_interval_minutes)
   │     • reads now → +sync_horizon_days via CalendarReader (in executor, off the API loop)
   │     • upserts into calendar_events; prunes events that vanished from the window
   │
   ├─ CalendarEventRepository (NEW — aiosqlite CRUD over calendar_events, migration v18)
   │
   └─ API routes (src/api/routes/calendar.py — extended)
         • GET  /api/calendar/events?start=&end=   → live CalendarReader read (UI agenda source)
         • POST /api/calendar/sync                 → trigger a manual mirror, returns count
         • GET  /api/calendar/calendars            → list calendars for the exclude UI
         • GET  /api/calendar/meetings             → (existing, unchanged — recorded meetings)
```

**Data flow.**

- _Agenda (UI):_ `CalendarView` fires a second query `getCalendarEvents(start, end)` in parallel with the existing recorded-meetings query, and merges both into the month/week/day/agenda grids.
- _Mirror (daemon):_ the scheduler job keeps `calendar_events` populated for the near-term window. **No UI consumer reads that table this phase** — it is the substrate auto-prep/auto-arming will read next, and is exercised now by repository + sync-job tests.

**Why live-for-UI + mirror-for-daemon** (not store-backed UI reads): the visible agenda stays always-fresh and correct for _any_ month navigated to, with zero coverage-gap logic, while still building the persistent foundation. Honest caveat: the new table has no UI consumer yet this phase.

## 5. Data model

**`CalendarEvent` dataclass** (returned by `CalendarReader`):

```python
event_uid: str        # synthesized stable key (see recurring-events note, §7)
title: str
start_ts: float       # unix
end_ts: float
attendees: list[dict] # [{"name","email"}], self excluded
organizer: dict | None
join_url: str         # Teams/Zoom/Meet, "" if none
meeting_id: str       # Teams thread id when present, "" otherwise
calendar_name: str    # source calendar (for display + exclude filtering)
```

**`calendar_events` table (migration v18):**

```sql
CREATE TABLE calendar_events (
    event_uid            TEXT PRIMARY KEY,   -- eventIdentifier + ":" + int(start_ts)
    title                TEXT NOT NULL,
    start_ts             REAL NOT NULL,
    end_ts               REAL NOT NULL,
    attendees_json       TEXT NOT NULL DEFAULT '[]',
    organizer_json       TEXT,
    join_url             TEXT NOT NULL DEFAULT '',
    meeting_id           TEXT NOT NULL DEFAULT '',
    calendar_name        TEXT NOT NULL DEFAULT '',
    recorded_meeting_id  TEXT,               -- nullable; set when a recording matches (reconciliation)
    synced_at            REAL NOT NULL
);
CREATE INDEX idx_calendar_events_start ON calendar_events(start_ts);
```

`recorded_meeting_id` is included now (cheap nullable column) as the bridge later phases use to answer "did we record this scheduled meeting?", populated opportunistically by matching `meeting_id`. Not otherwise written this phase.

**Config — `CalendarConfig` extended:**

```python
enabled: bool = False               # existing (matcher master switch)
time_window_minutes: int = 15       # existing (matcher)
min_confidence: float = 0.7         # existing (matcher)
import_enabled: bool = True         # NEW — gate the sync job + events route
sync_interval_minutes: int = 15     # NEW
sync_horizon_days: int = 21         # NEW — rolling mirror window
excluded_calendars: list[str] = []  # NEW — calendar names to skip
```

**Initialization gate.** Today the EventKit store is only created when `enabled` is true (matcher init). Because import must work independently of the reactive matcher, the shared `CalendarReader` (and its store/auth) is initialized when **`enabled` OR `import_enabled`** is true. `enabled` continues to gate the reactive `match()` path; `import_enabled` gates the sync job and the `/events` route. A user can therefore run import without the matcher, or vice versa.

## 6. Backend behaviour

**`CalendarReader` (extraction).** The EventKit access currently inside `CalendarMatcher._do_match` (store init, 60s auth wait, attendee/organizer extraction, Teams-URL parsing) moves into a shared `CalendarReader`. `CalendarMatcher` keeps its `match()` API and delegates EventKit access to the reader — **no behaviour change** to the reactive matching path, just deduplicated access. To stay CI-safe (EventKit is unavailable in tests), the reader separates **pure transforms** (raw-event → `CalendarEvent`, meeting-like predicate, `event_uid` synthesis, exclude filter) from EventKit I/O.

**`list_events(start, end)`** builds an EventKit predicate for the range, iterates events, skips all-day, applies the meeting-like and excluded-calendars filters, returns `CalendarEvent`s sorted by `start_ts`. Pure read.

**`CalendarSyncJob.run()`** (gated on `import_enabled`):

1. Compute window `[now, now + sync_horizon_days]`.
2. `events = await loop.run_in_executor(None, reader.list_events, now, now + horizon)` — the blocking EventKit call runs **off** the API loop.
3. Upsert each event (`INSERT … ON CONFLICT(event_uid) DO UPDATE`), stamping `synced_at`.
4. **Prune:** delete rows whose `start_ts` is in the window but whose `event_uid` was not in this fetch (cancelled/moved-away), **except** rows carrying a `recorded_meeting_id`.
5. Runs at boot and every `sync_interval_minutes`, wrapped in the scheduler's existing `safe_run` so a failure logs and never stalls other jobs.

## 7. Edge cases

- **Recurring events.** EventKit's `eventIdentifier` is _shared_ across all occurrences of a series, so it is not a unique key. `event_uid` is synthesized as `eventIdentifier + ":" + int(start_ts)` so each occurrence is a distinct row / agenda entry. Live reads sidestep persistence entirely.
- **Meeting-like filter** is defined once in `CalendarReader` so the live route and the sync job share it. Qualifies if `join_url` present **or** ≥2 non-self attendees; all-day events skipped.
- **Excluded calendars.** `GET /api/calendar/calendars` returns `[{id,title}]`; the Settings UI toggles them; excluded **names** persist into `CalendarConfig.excluded_calendars` via the existing config PUT route. Both reader and sync job filter on the list.

## 8. Error handling

Each condition degrades; none crashes the pipeline or scheduler.

| Condition                                       | Behaviour                                                                                                                                      |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| EventKit unavailable / not authorized           | Reader reports unavailable; `GET /events` returns `{events: [], count: 0}`; sync job no-ops with a debug log. UI shows recorded meetings only. |
| `calendar.import_enabled = false`               | Sync job not registered; `/events` returns empty.                                                                                              |
| EventKit read raises                            | Caught per-call; live route returns empty + logs; sync job's `safe_run` swallows + logs.                                                       |
| `start >= end` or range > 366 days on `/events` | 422 (mirrors the existing `/meetings` guards).                                                                                                 |
| DB write fails mid-sync                         | Logged; next interval retries (upsert is idempotent).                                                                                          |

**Auth:** the reader shares the daemon's existing single `EKEventStore` grant — no new permission prompt beyond the calendar access the matcher already requests.

## 9. Frontend

- **`lib/types.ts`** — add `CalendarEvent` type.
- **`lib/api.ts`** — `getCalendarEvents(start, end)`, `triggerCalendarSync()`, `getCalendars()`.
- **`CalendarView.tsx`** — a second `useQuery` for events in the visible range, parallel to the existing recorded-meetings query; both passed to the four grid components.
- **`MonthGrid` / `WeekTimeline` / `DayDetail` / `AgendaList`** — each gains an `events` prop alongside `meetings`. Upcoming events render **distinctly** (muted/dashed "scheduled" affordance), sorted by time within a day. Recorded-meeting click → existing meeting detail; upcoming-event click → a lightweight **event popover** (title, time, attendees, join link). Prep/record actions on that popover are deferred (§10).
- **Settings** — a new **Calendars** section (mirroring `AutomationsSection`/`InsightsSection`): fetches `getCalendars()`, a checkbox per calendar (checked = included), persists excluded **names** into `CalendarConfig.excluded_calendars` via the existing config PUT. Optional small "Sync now" button + last-synced hint.

## 10. Roadmap — later phases

These are **explicitly planned as the next work** after this foundation, each its own brainstorm → spec → plan → build cycle. Do not treat as dropped scope.

1. **Auto-generate prep briefings** ahead of meetings — wires the currently-orphaned `src/prep/briefing.py` to fire before an upcoming event using the imported calendar data.
2. **Auto-arm / auto-start recording** for scheduled calendar meetings.
3. **Event-popover prep/record actions** — turn the info-only popover into actionable controls.
4. **Dashboard "next up" widget** — compact at-a-glance upcoming meetings.

## 11. Testing

**Python** (EventKit-free, via fake event objects and a fake reader):

- `test_db_migration_v18.py` — table creation + idempotent upgrade (new migration head, replacing v17).
- `test_calendar_reader.py` — meeting-like filter, exclude filter, recurring `event_uid`, all-day skip.
- `test_calendar_event_repository.py` — upsert, prune (must **not** delete `recorded_meeting_id` rows), range query.
- `test_calendar_sync_job.py` — window calc, upsert+prune via fake reader, `import_enabled` gate, off-loop executor.
- `test_api_calendar.py` — `/events` live via fake reader, 422 guards, `/sync`, `/calendars`, empty-when-unavailable.
- `test_config.py` — new `CalendarConfig` fields.

**UI (vitest):**

- `api.test.ts` — new client fns.
- `CalendarView` — merges events + meetings, distinct rendering, event-click popover.
- Settings Calendars section — lists calendars, toggles persist excluded.
- `MonthGrid` / `AgendaList` — render events distinctly.

**Housekeeping:** update `config.example.yaml` with the new calendar fields.
