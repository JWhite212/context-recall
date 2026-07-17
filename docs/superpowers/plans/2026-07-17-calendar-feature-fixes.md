# Blueprint — Calendar feature: permission registration + duplicate-calendar/event fixes

**Objective:** Fix the two user-reported calendar defects — (1) the app never appears in
System Settings → Privacy → Calendars ("access is not granted", no way to add it), and
(2) duplicate calendars in the picker that toggle together and produce duplicate meetings —
and harden the wider calendar feature for correctness and efficiency.

**Created:** 2026-07-17
**Branch base:** `origin/main` @ `fc39190`
**Working branch:** `feat/calendar-fixes`
**Mode:** git + gh (draft PR per step group)
**Author preferences:** strict TDD (test first), one fix per commit, "what I did NOT do"
call-outs, no Claude/AI attribution in commits or PR bodies.

---

## Root-cause analysis (evidence-backed)

Gathered live from the deployed daemon (pid 1337) and its logs on 2026-07-17.

### The permission symptom is a _code_ bug, not (only) the macOS beta

- `GET /api/calendar/permission` (live) → `{"status":"not_determined","granted":false}`.
- `GET /api/calendar/calendars` (live) → `{"calendars":[]}`.
- Installed daemon bundle passes `codesign --verify --deep --strict` (exit 0), has a stable
  cert-leaf DR, and **zero** `__pycache__` seal-breakers. So this is _not_ a bundle-validation
  failure like the mic case.
- Boot log shows calendar status **flapping**: `authorized` at 11:52/16:52/12:01 but
  `not_determined` at most other boots.
- The log is full of, every few minutes and sometimes 4× in 7 seconds:
  `EKCADErrorDomain Code=1021 "This process has too many EKEventStore instances. Use fewer event stores."`

macOS Privacy → Calendars has **no "+" button** — an app appears there only after it
successfully _requests_ access (tccd registers it on the request). Because the daemon
exhausts its EventKit store budget, the request never cleanly completes, tccd never finalises
the registration, and the user therefore cannot add the app manually. That is the screenshot.

### B1 (CRITICAL) — `EKEventStore` instance leak → EventKit exhaustion

Three independent `EKEventStore.alloc().init()` sites:

- `src/calendar_permission.py:93` — `request_access()` allocates a **new** store every call.
- `src/calendar_events/reader.py:139` — `CalendarReader._create_store()`.
- `src/calendar_matcher.py:194` — `CalendarMatcher._create_store()`.

Amplifier: `CalendarReader._ensure_store()` deliberately does **not** latch on a
`not_determined` result (see `test_reader_retries_after_unanswered_request`), so **every**
`list_events` / `list_calendars` call re-fires `request_access(timeout=60)` — creating another
store each time — while the status stays `not_determined`. These calls come from:

- the Settings page (`getCalendars` + `getCalendarPermission`, refetched on mount/focus),
- `CalendarView` (`getCalendarEvents` per view + 84-day heatmap, `staleTime` 30 s),
- the `calendar_sync` scheduler job every 15 min,
- the auto-arm `_calendar_source` tick.

Each blocks up to 60 s on an executor thread while holding `_ensure_store`'s `_init_lock`,
so threads pile up (B8). Stores accumulate until macOS caps the process → **all** EventKit
calls start failing with 1021 → `authorization_status()` reads garbage (flaps to
`not_determined`) → the grant can never stabilise. The boot log proves the OS _can_ grant
calendar to this daemon (unlike mic on the beta) — the leak corrupts it afterwards. **Fixable
in code.**

**Fix:** one process-wide shared `EKEventStore` (module singleton in `calendar_permission`,
`get_shared_store()`), reused by `request_access`, reader, and matcher. Add a re-request
cooldown so the reader can't hammer `request_access` while `not_determined`. Keep the boot
poller as the primary requester. Reset the singleton between tests via a conftest fixture.

### B2 (CRITICAL — the user's "duplicate calendars" report) — exclusion keyed by title

- `ui/src/components/settings/CalendarsSection.tsx` toggles and checks by `c.title`
  (`toggle(c.title, …)`, `checked = !excluded.includes(c.title)`), even though the React
  `key` is `c.id`.
- `src/calendar_events/reader.py:_events_from_extracted` filters `excluded_calendars` by
  `calendar_name` (title).

Two **distinct** calendars sharing a title (extremely common: "Calendar", "Home",
"Birthdays", or the same account name across iCloud + Google) share one exclusion state:
ticking one ticks all, and excluding the title drops events from all of them. This is exactly
"selecting one selects all the duplicates."

**Fix:** key exclusion by calendar **id** end to end. `_extract` captures
`calendar_identifier`; add `CalendarEvent.calendar_id`; `_events_from_extracted` excludes if
`calendar_id ∈ excluded` **or** `calendar_name ∈ excluded` (backward-compatible with any
legacy title entries — the live prod config currently has none). UI toggles/checks `c.id`.
**No DB migration** — exclusion is applied at read time before the mirror upsert, and the
repository writes an explicit column list, so `calendar_id` can be a read-only dataclass field.

### B3 (HIGH — the user's "duplicate meetings" report) — no cross-calendar event dedup

`list_events` reads **all** calendars (`predicateFor…calendars_(…, None)`). The same underlying
meeting present on two calendars (duplicate accounts, or an invite mirrored across accounts)
yields two `EKEvent`s with distinct `eventIdentifier`s → distinct `event_uid`
(`<eventIdentifier>:<int(start_ts)>`) → both render. That is "duplicated meetings in the
calendar section."

**Fix:** dedupe in `_events_from_extracted` by a content key — prefer `meeting_id`, else
`join_url`, else `(title.strip().lower(), int(start_ts), int(end_ts))`. Keep the first
occurrence (post-exclusion) so a non-excluded copy wins.

### B4 (MEDIUM) — identical calendar titles are indistinguishable in the picker

Even with id-based toggling, two rows labelled "Calendar" are confusing. `EKCalendar.source()`
gives the account ("iCloud", "Google", "Exchange").

**Fix:** `list_calendars` also returns `source`; UI renders "Title — Source" when a title is
non-unique.

### B5 / B8 (LOW) — request path duplicated + `_init_lock` held across a 60 s blocking call

`CalendarReader._ensure_store` and `CalendarMatcher._init_store` both can fire the (blocking,
prompt-raising) `request_access`, duplicating the boot poller and serialising readers behind a
60 s wait. Folded into B1: the boot poller is the sole requester; readers/matchers only attach
the shared store when authorized and self-heal.

### B6 (MEDIUM — direct fix for "I cannot add the app") — no in-app "Request access"

The Settings banner only offers "Open System Settings", which is useless for a
`not_determined` app that isn't in the list. After B1, the app _can_ register — but the user
needs a way to (re)trigger the prompt from the daemon.

**Fix:** `POST /api/calendar/request` fires `calendar_permission.request_access` on a worker
thread (idempotent, non-blocking to the loop); the banner gains a "Request calendar access"
button and explanatory copy. Keep "Open System Settings" as the secondary action for the
denied case.

### B7 (LOW, efficiency) — `load_config()` on every calendar request

`get_calendar_events` / `sync_calendar` call `load_config()` (disk read + YAML parse) per
request. Read from the injected server config instead.

---

## Invariants (verify after every step)

- `python3 -m pytest tests/ -q` stays green (~1229 baseline).
- `ruff check src/ tests/` clean.
- `cd ui && npm test` and `npx tsc --noEmit` clean.
- No test triggers a real TCC dialog (the conftest guard must still pass).
- The daemon still imports with EventKit absent (CI): `python3 -c "from src.main import ContextRecall"`.

---

## Step 1 — Single shared EKEventStore + request cooldown (B1, B5, B8) [CRITICAL, strongest model]

**Context brief.** The daemon leaks `EKEventStore` instances until macOS returns
`EKCADErrorDomain Code=1021`, after which all EventKit calls (including
`authorizationStatusForEntityType_`) fail and the calendar grant can never finalise. Collapse
all store creation onto one shared instance and stop the reader re-firing the access request.

**Files:** `src/calendar_permission.py`, `src/calendar_events/reader.py`,
`src/calendar_matcher.py`, `tests/conftest.py`, `tests/test_calendar_permission.py`,
`tests/test_calendar_reader.py`, `tests/test_calendar_matcher.py`.

**Tasks (TDD — test first each):**

1. `calendar_permission.get_shared_store()`: lazily `alloc().init()` **once** under a lock,
   cache in a module global, return the cached instance thereafter; return `None` when
   EventKit is unavailable. Add `reset_shared_store()` for tests.
2. `request_access()` uses `get_shared_store()` instead of `alloc().init()`.
3. `CalendarReader._create_store()` and `CalendarMatcher._create_store()` attach the shared
   store (`calendar_permission.get_shared_store()`), not a fresh one.
4. `CalendarReader._ensure_store()`: keep the "retry after grant" behaviour, but add a
   monotonic cooldown (e.g. 30 s) so a `not_determined` status re-fires `request_access` at
   most once per cooldown window instead of on every call. Do **not** hold `_init_lock` across
   the blocking request beyond what's necessary.
5. `tests/conftest.py`: autouse fixture calls `calendar_permission.reset_shared_store()` so
   the singleton never leaks across tests; keep the existing "request_access raises" guard.
6. Update `test_calendar_reader.py` cooldown-sensitive tests (the "retries after unanswered
   request" case now asserts _cooldown-bounded_ retries, not per-call).

**Verify:** `pytest tests/test_calendar_permission.py tests/test_calendar_reader.py
tests/test_calendar_matcher.py -q`; grep the code to prove only `get_shared_store` calls
`alloc().init()`.

**Exit criteria:** exactly one `EKEventStore.alloc().init()` call site in the codebase;
reader/matcher/permission all share it; reader cannot fire `request_access` more than once per
cooldown while `not_determined`; full suite green.

**Rollback:** revert the commit; behaviour returns to per-call stores (leak).

---

## Step 2 — Calendar exclusion keyed by id, not title (B2) [depends on nothing; parallel with 1]

**Context brief.** Duplicate-titled calendars share exclusion state because exclusion is keyed
by title in both the reader and the UI. Move to calendar id with a title fallback for
backward compatibility. No DB migration (read-time filter; explicit-column repository).

**Files:** `src/calendar_events/reader.py`, `src/api/routes/calendar.py` (unchanged shape —
already returns `{id,title}`), `ui/src/components/settings/CalendarsSection.tsx`,
`tests/test_calendar_reader.py`, `ui/src/components/settings/__tests__/CalendarsSection.test.tsx`.

**Tasks (TDD):**

1. `CalendarEvent` gains `calendar_id: str = ""`. `_extract` captures
   `event.calendar().calendarIdentifier()` as `calendar_identifier`.
2. `_events_from_extracted`: exclude when `e["calendar_identifier"] ∈ excluded` **or**
   `e["calendar_name"] ∈ excluded` (legacy). Populate `CalendarEvent.calendar_id`.
3. UI: `toggle(c.id, …)`, `checked = !excluded.includes(c.id)`.
4. Tests: exclusion by id skips only the matching id; two calendars sharing a title but
   differing in id are independently excludable; a legacy title entry still excludes.

**Verify:** `pytest tests/test_calendar_reader.py -q`; `cd ui && npm test -- CalendarsSection`.

**Exit criteria:** excluding one of two same-titled calendars leaves the other's events
visible; legacy title-based exclusions still honoured.

**Rollback:** revert; exclusion returns to title keying.

---

## Step 3 — Cross-calendar event de-duplication + source disambiguation (B3, B4) [depends on Step 2]

**Context brief.** The same meeting on two calendars renders twice; identical calendar titles
are indistinguishable in the picker.

**Files:** `src/calendar_events/reader.py`, `src/api/routes/calendar.py`,
`ui/src/components/settings/CalendarsSection.tsx`, tests for each.

**Tasks (TDD):**

1. `_events_from_extracted` dedup by content key (meeting_id → join_url →
   `(title.strip().lower(), int(start_ts), int(end_ts))`), keeping the first post-exclusion
   occurrence. Preserve start-time sort.
2. `list_calendars` returns `source` from `c.source().title()` (guarded).
3. UI shows "Title — Source" when the title is non-unique among returned calendars.

**Verify:** `pytest tests/test_calendar_reader.py tests/test_api_calendar.py -q`;
`cd ui && npm test -- CalendarsSection`.

**Exit criteria:** duplicate meetings collapse to one; same-titled calendars are
distinguishable by account.

**Rollback:** revert; duplicates and ambiguous titles return.

---

## Step 4 — In-app "Request calendar access" (B6) [depends on Step 1]

**Context brief.** A `not_determined` app cannot be added in System Settings; the user needs
the daemon to fire the prompt. After Step 1 the request can register cleanly.

**Files:** `src/api/routes/calendar.py`, `src/api/server.py` (route already wired),
`ui/src/components/settings/CalendarsSection.tsx`, `ui/src/lib/api.ts`,
`tests/test_api_calendar.py`, `CalendarsSection.test.tsx`.

**Tasks (TDD):**

1. `POST /api/calendar/request` → `run_in_executor(None, calendar_permission.request_access)`;
   return the resulting status. Idempotent; safe when EventKit unavailable.
2. UI banner: primary "Request calendar access" (calls the new endpoint, then invalidates the
   permission query), secondary "Open System Settings"; copy explains that macOS will show a
   prompt and the app appears in the list only after requesting.

**Verify:** `pytest tests/test_api_calendar.py -q`; `cd ui && npm test -- CalendarsSection`.

**Exit criteria:** clicking "Request calendar access" triggers the prompt; on grant the banner
clears and calendars load.

**Rollback:** revert; banner returns to Settings-only.

---

## Step 5 — Efficiency + tidy (B7) [optional; parallel]

Read calendar config from the injected server config rather than `load_config()` per request;
remove now-dead request paths left by Step 1; refresh `CLAUDE.md`/memory notes on the calendar
architecture.

**Verify:** full Python + UI suites + ruff + tsc.

---

## Suggested PR grouping

- **PR A (critical):** Step 1 + Step 4 — "fix calendar permission registration" (the
  screenshot). Ship first.
- **PR B:** Step 2 + Step 3 — "fix duplicate calendars and duplicate meetings".
- **PR C (optional):** Step 5 tidy.

Or a single `feat/calendar-fixes` PR if the reviewer prefers one unit. Draft PR either way;
manual signed-build verification on device required before merge (TCC behaviour can't be
tested in CI).

## Review gate

Whole-branch adversarial review (opus) before marking ready, focused on: EventKit store
lifetime/thread-safety, the exclusion backward-compat path, dedup key collisions (distinct
meetings that happen to share title+time), and that no test can fire a real TCC prompt.
