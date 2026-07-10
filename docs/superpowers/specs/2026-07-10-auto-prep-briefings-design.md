# Auto-Prep Briefings — Design (Track B, Phase 2)

**Date:** 2026-07-10
**Track:** B — "Calendar hub", Phase 2 (auto-prep briefings)
**Depends on:** the Track B foundation (`calendar_events` table + scheduler + `CalendarEventRepository`), currently on branch `feat/calendar-import` (unmerged). **This phase branches off `feat/calendar-import`, not `main`.**

## 1. Problem & context

The foundation now imports upcoming calendar events and mirrors a near-term window into `calendar_events`. Separately, a full `PrepBriefingGenerator` (`src/prep/briefing.py`) exists — it gathers context from meeting history (series + attendee history + open action items) and produces a Markdown briefing — but it is **orphaned**:

- The server only wires `prep_routes.init(prep_repo)` with **no generator**, so even the manual `POST /api/prep/{meeting_id}/generate` returns **503** ("Briefing generator not available").
- Nothing generates a briefing _ahead of_ a meeting, because until the foundation nothing knew a meeting was coming.
- `prep_briefings` is keyed by recorded `meeting_id`/`series_id` with a 2-hour TTL — there is **no link to an upcoming calendar event**.

Phase 2 closes this: a scheduler-driven sweep pre-generates a briefing for each qualifying upcoming event, links it to the event, and surfaces it in the UI — so prep is ready before the meeting.

## 2. Goals / non-goals

**Goals:**

- A background sweep that pre-generates briefings for upcoming _context-rich_ calendar events within a lookahead window.
- Wire the (currently orphaned) `PrepBriefingGenerator` into the server so both the sweep and the manual route work.
- Link briefings to their upcoming event; regenerate when the event materially changes.
- Surface briefings: an "Upcoming briefings" list in the Prep view **and** a read-only "prep ready" badge on upcoming calendar events.
- Degrade gracefully (LLM failure, no calendar data, feature disabled) without crashing the scheduler.

**Non-goals (phase 3+):** interactive prep/record actions on the event popover, auto-arming/starting recording, and the dashboard "next up" widget.

## 3. Decisions (from brainstorming)

| Decision     | Choice                                                                                                                                 |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Trigger      | **Scheduler pre-generates ahead** — a dedicated `prep_sweep` job (not folded into `calendar_sync`).                                    |
| Which events | **Only context-rich events** — attendee-history match **or** series title-match.                                                       |
| Lookahead    | **Next 24h** (configurable), **regenerate on change** (time via uid, attendees via signature).                                         |
| UI           | **Both** — extend the Prep view to a list **and** a read-only "prep ready" badge on calendar events (interactive actions are phase 3). |
| Mechanism    | Dedicated `PrepSweepJob`; reject folding into `calendar_sync` (mixes fast sync with slow LLM under one 60s timeout).                   |

## 4. Architecture

```
Scheduler (ApiServer)
   │
   ├─ prep_sweep job (NEW — registered in _setup_scheduler_jobs, gated on
   │     prep.auto_generate AND calendar.import_enabled; interval sweep_interval_minutes;
   │     lambda: safe_run("prep_sweep", self._sweep_prep, timeout=300))
   │     1. events = CalendarEventRepository.list_by_range(now, now + lookahead_hours*3600)
   │     2. skip events with a current briefing (has_current_for_event(uid, signature))
   │     3. keep only context-rich events (_qualifies: attendee-history OR series title-match)
   │     4. generate up to max_per_sweep per tick via PrepBriefingGenerator (LLM offloaded);
   │        link each to the event (calendar_event_uid, event_signature, expires_at=event.end_ts)
   │
   ├─ PrepBriefingGenerator (EXISTING — now WIRED with PrepConfig + SummarisationConfig + repos;
   │     generate() offloads its blocking LLM chat via run_in_executor)
   │
   ├─ PrepRepository (EXTENDED): create(... calendar_event_uid, event_signature),
   │     get_by_calendar_event(uid), list_upcoming(limit),
   │     has_current_for_event(uid, signature), prepared_event_uids()
   │
   └─ prep_briefings table (migration v19): + calendar_event_uid TEXT, + event_signature TEXT
         (index on calendar_event_uid)
```

**Data flow.**

- _Generation (daemon):_ the sweep reads upcoming events from `calendar_events`, filters to context-rich ones without a current briefing, and generates ahead of time — nothing user-triggered.
- _Regen-on-change:_ an event's `event_uid` encodes its start (`identifier:int(start_ts)`), so a **time change yields a new uid** → fresh briefing; the stale one expires at its old `end_ts`. **Attendee changes** are caught by comparing `event_signature` (hash of sorted lowercased attendee emails).
- _Surfacing (UI):_ the Prep view calls `GET /api/prep/upcoming-list`; the calendar fetches `GET /api/prep/prepared-events` and badges any `UpcomingEventCard` whose uid has a current briefing, linking to `/prep` (read-only).

## 5. Data model & config

**`prep_briefings` (migration v19):**

```sql
ALTER TABLE prep_briefings ADD COLUMN calendar_event_uid TEXT;   -- nullable; links to calendar_events
ALTER TABLE prep_briefings ADD COLUMN event_signature TEXT;      -- hash of sorted lowercased attendee emails
CREATE INDEX IF NOT EXISTS idx_prep_briefings_cal_event ON prep_briefings(calendar_event_uid);
```

(Columns added via `_safe_add_column(conn, "prep_briefings", ...)` — `prep_briefings` is **already** in `_ALLOWED_TABLES`, so no change there. The index goes in a `<NAME>_SQL`-style statement. Applied in both the fresh-install `< 1` block and a new `if current_version < 19:` block, per the migration convention; move the trailing `else: logger.debug(...)` after the new block.)

For auto-briefings, `expires_at` is set to the event's `end_ts` (valid through the meeting; expires after). Manual/series briefings keep the existing 2h default and leave the two new columns null.

**`PrepConfig` (extended):**

```python
auto_generate: bool = False        # NEW — master switch for the sweep
lookahead_hours: int = 24          # NEW
sweep_interval_minutes: int = 15   # NEW
max_per_sweep: int = 5             # NEW — cap LLM generations per tick (cost + timeout safety)
# existing: max_context_meetings, max_attendee_history, (etc.)
```

**The "context-rich" filter** (`_qualifies(event)` returns true if **either**):

1. **Attendee history** — at least one of the event's attendee emails appears in a prior completed meeting (reuses `MeetingRepository.list_recent_complete_with_attendees` + email overlap, the same lookup `gather_context` uses), **or**
2. **Series title-match** — the event's normalized title matches an existing `meeting_series` title.

Open action items do **not** by themselves qualify an event (they're global, not event-specific), but they **are** injected into the briefing context when the event qualifies via (1) or (2). A brand-new contact with an unrecognized title is skipped.

**`event_signature`** = a stable hash (e.g. sha1) of the event's sorted, lowercased attendee emails. `has_current_for_event(uid, signature)` returns true iff a non-expired briefing exists for that `calendar_event_uid` with a matching `event_signature`.

## 6. Sweep behaviour & generator wiring

**Wire the generator (fixes the manual path too):** in `_create_app`, instantiate `PrepBriefingGenerator(config=prep, summarisation_config=summarisation, meeting_repo, action_item_repo, series_repo, prep_repo)`, store as `self._prep_generator`, and change `prep_routes.init(prep_repo)` → `prep_routes.init(prep_repo, self._prep_generator)`.

**Offload the LLM call:** `PrepBriefingGenerator.generate()` currently calls the blocking `_ollama_chat`/`_claude_chat` directly. Change that single call to `await asyncio.get_running_loop().run_in_executor(None, ...)` so the method never blocks the event loop from the async sweep (or the manual route). Behaviour otherwise unchanged. `generate()` gains an optional `calendar_event_uid` + `event_signature` + `expires_at` so the sweep can link and set expiry; existing callers omit them.

**`_sweep_prep()`** (async method on `ApiServer`, gated on `prep.auto_generate AND calendar.import_enabled`):

1. `events = await cal_event_repo.list_by_range(now, now + lookahead_hours*3600)`.
2. For each event: compute `signature`; **skip** if `await prep_repo.has_current_for_event(uid, signature)`.
3. **Skip** if not `_qualifies(event)`.
4. Generate, at most `max_per_sweep` per tick, via `self._prep_generator.generate(title, attendees, attendee_names, series_id=<matched or None>, calendar_event_uid=uid, event_signature=signature, expires_at=event.end_ts)`. Remaining qualifying events wait for the next tick.

## 7. Error handling

Each condition degrades; none crashes the scheduler.

| Condition                                                                              | Behaviour                                                                                                           |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `prep.auto_generate` off, `calendar.import_enabled` off, or generator/cal-repo missing | Job not registered / no-ops with a debug log                                                                        |
| LLM call fails for one event                                                           | `generate()`'s existing `_build_fallback` produces a thin briefing from gathered context, still linked to the event |
| One event's generation raises                                                          | Caught per-event, logged; the sweep continues to the next event                                                     |
| Batch runs long                                                                        | `max_per_sweep` cap + 300s `safe_run` timeout bound each tick; leftovers next tick                                  |
| No `calendar_events` in range                                                          | Empty range → no-op                                                                                                 |

Expired briefings are filtered by the existing `expires_at > now` reads, so stale/moved-event briefings fall out after their event ends — no separate pruner this phase.

## 8. API

Extend `src/api/routes/prep.py`:

- `GET /api/prep/upcoming-list?limit=` → current (non-expired) auto-briefings, newest-first, for the Prep view (`PrepRepository.list_upcoming(limit)`).
- `GET /api/prep/prepared-events` → `{ "event_uids": [...] }`, the set of `calendar_event_uid`s with a current non-expired briefing, for the badge. (Its own endpoint so the foundation's live `/api/calendar/events` route stays DB-free.)
- `POST /api/prep/{meeting_id}/generate` — unchanged code, but now functional (generator wired).

## 9. Frontend

- `lib/types.ts`: a `PrepBriefing` list shape + `{ event_uids: string[] }`.
- `lib/api.ts`: `getUpcomingPrepList(limit?)`, `getPreparedEventUids()`.
- **Prep view** (`components/prep/PrepBriefing.tsx`, `/prep`): extend from single-latest to an **"Upcoming briefings" list** (a card per near-term event with its Markdown).
- **Badge:** `CalendarView` adds a parallel query for prepared-event uids and threads an optional `preparedUids: Set<string>` into the grids → `UpcomingEventCard` renders a small read-only **"Prep ready"** badge when `preparedUids.has(event.event_uid)`, linking to `/prep`. No interactive actions.

## 10. Testing

**Python** (LLM/models stubbed — no real model loads):

- `test_db_migration_v19` — new columns on `prep_briefings` + upgrade from v18 preserves data (new migration head).
- extend `test_prep_repository` — `calendar_event_uid`/`event_signature` create; `get_by_calendar_event`; `list_upcoming`; `has_current_for_event` (match true / signature-diff false / expired false); `prepared_event_uids`.
- `test_prep_sweep_job` — fake generator + real repos + seeded `calendar_events`: generates for context-rich, skips cold, skips already-briefed, regenerates on signature change, `max_per_sweep` cap, config gating; the attendee-history + series-title `_qualifies` logic.
- extend `test_prep_briefing` — generate() with a stubbed summariser offloads via executor and writes `calendar_event_uid`/`event_signature`/`expires_at`.
- extend `test_api_prep` — `upcoming-list`, `prepared-events`, and manual generate now returns 201 (generator wired).
- `test_config` — new `PrepConfig` defaults.
- server-wiring test — `prep_sweep` registered when `auto_generate && import_enabled`, absent otherwise.

**UI (vitest):** `api.test` (new client fns); Prep view (renders a list of upcoming briefings); `CalendarView`/`UpcomingEventCard` (badge shows when uid ∈ `preparedUids`).

**Housekeeping:** `config.example.yaml` prep fields.

## 11. Roadmap — later phases

Unchanged from the foundation spec's roadmap; still explicitly planned, each its own spec→plan→build:

1. **Event-popover prep/record actions** — turn the read-only badge into interactive controls (open/regenerate prep, start recording).
2. **Auto-arm / auto-start recording** for scheduled calendar meetings.
3. **Dashboard "next up" widget.**
