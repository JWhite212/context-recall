# Design: Link a recorded meeting to a calendar entry

**Date:** 2026-07-21
**Branch:** `feat/calendar-recording-link` (off `main`)
**Status:** Approved design — ready for implementation planning

## Problem

The Calendar screen shows a recorded meeting and its originating calendar entry as
**two separate cards** for the same real-world call. In the observed case, the
recording _"Amelia Monthly Check-In Meeting"_ (a `Meeting`, solid dot, rendered by
`EventCard`) and the calendar entry _"Jamie - Quick Catch-Up"_ (a `CalendarEvent`,
dashed border, rendered by `UpcomingEventCard`) are the same meeting but appear twice.

The user wants to **apply / assign a manually-recorded meeting onto a calendar entry**
so the recording is durably associated with that entry, the duplicate collapses, and
the recording adopts the calendar entry's context (attendees, event title, Teams link).

## Current state (why this is half-built already)

Two tables, two repositories, joined only visually in the UI:

- **`meetings`** — recorded meetings. Carries _denormalized copies_ of calendar data
  populated once at record time by the live matcher: `calendar_event_title`,
  `attendees_json`, `calendar_confidence`, `teams_join_url`, `teams_meeting_id`.
  There is **no** `calendar_event_uid` column — no referential link to a calendar entry.
- **`calendar_events`** — the synced EventKit mirror. PK `event_uid` =
  `f"{eventIdentifier}:{int(start_ts)}"`. Already has a nullable `recorded_meeting_id`
  column **and** a repository method `set_recorded_meeting(event_uid, meeting_id)` — but
  **nothing calls it** (dead code, tests only). `prune_window` already spares rows whose
  `recorded_meeting_id IS NOT NULL`.

Two independent ingestion paths that never cross-key:

1. `CalendarMatcher.match(started_at)` → `CalendarMatch` (live EventKit, at record time).
   Fills the meeting's denormalized calendar fields. `CalendarMatch` carries the Teams
   ids and `event_start`/`event_end` but **no `event_uid`**.
2. `CalendarReader` → `CalendarSyncJob` fills the `calendar_events` mirror on a schedule.

UI: `CalendarView` runs two independent queries — `['calendar', start, end]` (recorded
meetings) and `['calendar-events', start, end]` (calendar entries) — and renders both,
with no dedup. `EventCard` has no popover (click → `/meetings/{id}`); `UpcomingEventCard`
owns the interactive prep/record popover. Meeting-edit pattern is well established:
`PATCH /api/meetings/{id}/...` → `update_meeting(**fields)` (validated against
`_MUTABLE_COLUMNS`) → invalidate `['meeting', id]` + `['meetings']`.

## Decisions (agreed)

1. **Behaviour:** link **and adopt calendar-derived metadata** onto the recording,
   non-destructively. No reprocess, no note/Notion rewrite.
2. **Entry points:** both sides, via a picker — a `⋯` menu on the recorded card and an
   "Assign a recording" action on the calendar-entry popover (plus link/unlink on
   Meeting Detail).
3. **Calendar view:** **collapse** the duplicate into a single card (the recorded
   meeting, annotated with the linked entry's title).
4. **Auto-link now:** new recordings self-link at record time (the live matcher carries
   an `event_uid`), in this same change.

## Architecture

### 1. Data model — migration v24

Add one column to `meetings`:

```
calendar_event_uid TEXT DEFAULT ''
```

This is the **forward link** and the UI's source of truth. `meetings` is in
`_ALLOWED_TABLES`, so `_safe_add_column` handles it. No change to `calendar_events`
(its `recorded_meeting_id` already exists).

Bookkeeping:

- `SCHEMA_VERSION` 23 → **24**; add `tests/test_db_migration_v24.py`.
- Add `'calendar_event_uid'` to `_MUTABLE_COLUMNS` (`src/db/repository.py`).
- Add `calendar_event_uid` to the `MeetingRecord` dataclass, `from_row`, and `to_dict`.
- Add `calendar_event_uid?: string` to the UI `Meeting` type.

**Invariants** (enforced by the link service):

- One meeting ↔ at most one event (`meetings.calendar_event_uid`).
- One event ↔ at most one recording (`calendar_events.recorded_meeting_id`).
- Re-linking a meeting to a new event **moves** it — clears the previously-linked
  event's `recorded_meeting_id`.
- Linking to an event already tied to a **different** recording → **409 conflict**
  (caller must unlink the other first). Never silently steals it.

### 2. Link service — `src/calendar_link.py`

A small, pure-logic, unit-testable service that both the manual API endpoint and the
auto-linker call, so the two paths cannot drift.

```
async def link_meeting_to_event(meeting_repo, calendar_event_repo, meeting_id, event, *, source) -> None
async def unlink_meeting_from_event(meeting_repo, calendar_event_repo, meeting_id) -> None
```

`event` is a normalized value object (the `CalendarEvent` fields: `event_uid`, `title`,
`start_ts`, `end_ts`, `attendees`, `organizer`, `join_url`, `meeting_id`,
`calendar_name`). `source` is `"manual"` or `"auto"`.

**`link_meeting_to_event`:**

1. Load the meeting (404 if missing). Read its current `calendar_event_uid` (the "old"
   link, if any).
2. Conflict check: if `event.event_uid` is already `recorded_meeting_id`-linked to a
   **different** meeting → raise `CalendarLinkConflict` (→ 409).
3. `update_meeting` with `calendar_event_uid = event.event_uid` **and** the adopted
   calendar-derived fields (see rules below).
4. Upsert `event` into the `calendar_events` mirror (so the row exists even if not yet
   synced) and call `set_recorded_meeting(event.event_uid, meeting_id)`.
5. If the old link pointed at a different `event_uid`, clear that old event's
   `recorded_meeting_id` (move semantics).

**`unlink_meeting_from_event`:** clear `meetings.calendar_event_uid` and, if it pointed
at an event, clear that event's `recorded_meeting_id`.

**Adoption rules (non-destructive).** An explicit human link is authoritative for
_calendar-derived_ data; _user-authored_ data is preserved.

- **Refresh** (overwrite): `calendar_event_title` ← `event.title`;
  `attendees_json` ← `event.attendees`; `teams_join_url` ← `event.join_url`;
  `teams_meeting_id` ← `event.meeting_id`; `calendar_confidence` ← `1.0`.
- **Preserve** (never touched by linking): the meeting's display **`title`** (whether
  `auto` or `manual`), `tags`, `client_id`/`project_id` assignment, speaker mappings,
  `summary_markdown`, and the exported markdown/Notion note.
- **No reprocess.** Attendees are adopted as _data_ on the meeting row but are **not**
  re-seeded into speaker labels/diarisation — that remains the job of the existing
  Reprocess action, which the user can run if they want attendees applied to speakers.

> Rationale for overwriting `attendees_json` rather than fill-if-empty: the user is
> explicitly asserting "this recording IS this calendar entry," so the entry's attendee
> list is the correct one even if a weak auto-match had populated a different set.

Failures in steps 4–5 (mirror upsert / reverse link) are **non-fatal** on the auto path
(the forward link on the meeting is the source of truth); on the manual path they surface
as errors.

### 3. Auto-link at record time

- Add `event_uid: str = ""` to the `CalendarMatch` dataclass (`src/calendar_matcher.py`).
- In `CalendarMatcher._do_match`, when a candidate wins, compute its `event_uid` in the
  **same format the mirror reader uses** — `f"{eventIdentifier}:{int(start_ts)}"` — so an
  auto-link and the calendar view agree on identity. (Verify the EventKit attribute name
  during TDD; the reader uses `eventIdentifier`.)
- `main.py._process_audio` adds `calendar_event_uid` to the `calendar_fields` dict.
- `PipelineRunner` persists it (calendar_fields already flow to `update_meeting`) and,
  best-effort/non-fatal, calls the reverse-link half of the service (upsert +
  `set_recorded_meeting`) using the match data.
- `reprocess.py` re-supplies `calendar_event_uid` (alongside the existing
  `calendar_event_title`) so the link survives re-runs.

### 4. API (meetings router — inherits bearer auth)

- **`PUT /api/meetings/{meeting_id}/calendar-link`**
  Body: a `CalendarEventPayload` Pydantic model mirroring `CalendarEvent`
  (`event_uid`, `title`, `start_ts`, `end_ts`, `attendees`, `organizer`, `join_url`,
  `meeting_id`, `calendar_name`). The picker already holds this object, so **no live
  EventKit round-trip** on link. Calls `link_meeting_to_event(..., source="manual")`.
  Emits a `meeting.calendar_link` WS event. Returns the updated meeting (`to_dict()`).
  → 404 unknown meeting, 409 event already linked elsewhere, 422 bad payload.
- **`DELETE /api/meetings/{meeting_id}/calendar-link`**
  Calls `unlink_meeting_from_event`. Emits `meeting.calendar_link`. Returns
  `{meeting_id, calendar_event_uid: ""}`. → 404 unknown meeting.
- **Pickers reuse existing GETs** — no new query endpoints:
  - Recorded → entry picker: `GET /api/calendar/events?start&end` over a window around
    the recording's time; client filters to entries not already linked.
  - Entry → recording picker: `GET /api/calendar/meetings?start&end` over a window around
    the entry's time; client filters to recordings with empty `calendar_event_uid`.

### 5. UI

- **Dedup / collapse.** `CalendarView` computes
  `linkedEventUids = new Set(meetings.map(m => m.calendar_event_uid).filter(Boolean))`
  and threads it to every sub-view (`DayDetail`, `WeekTimeline`, `MonthGrid`,
  `AgendaList`). Each view drops any `CalendarEvent` whose `event_uid ∈ linkedEventUids`.
  The surviving `EventCard` renders a small **"↳ linked to «entry title»"** annotation
  (from the meeting's `calendar_event_title` when `calendar_event_uid` is set).
- **Recorded side** (`EventCard`, full mode): add a lightweight `⋯` menu →
  "Link to calendar event" → `CalendarLinkPicker` of nearby unlinked entries.
- **Calendar-entry side** (`UpcomingEventCard` popover): add **"Assign a recording"** →
  `CalendarLinkPicker` of nearby unlinked recordings.
- **Meeting Detail** calendar-match card: when linked, show "Linked to: «entry»" +
  **Unlink**; when not linked, a **"Link to calendar event"** button → picker.
- **`CalendarLinkPicker`** — one reusable, time-anchored, searchable list of nearby
  unlinked candidates, with two thin wrappers (pick-an-event, pick-a-recording).
- **API client** (`ui/src/lib/api.ts`): `linkMeetingToCalendarEvent(meetingId, event)`
  and `unlinkMeetingFromCalendarEvent(meetingId)`.
- **Cache.** Link/unlink mutations invalidate `['meeting', id]`, `['meetings']`,
  `['calendar']`, and `['calendar-events']`. Add `['calendar-events']` to the
  record-time (`pipeline.complete`) invalidation in `App.tsx` so auto-linked recordings
  self-collapse. Handle the new `meeting.calendar_link` WS event for cross-client refresh.

## Testing

**Python:**

- `tests/test_db_migration_v24.py` — column added, default `''`, idempotent, existing rows.
- Repository — `calendar_event_uid` round-trips via `update_meeting`/`get_meeting`/`to_dict`.
- `tests/test_calendar_link.py` — link adopts calendar-derived fields, preserves title/tags/
  assignment; sets forward + reverse link; move semantics clear the old event; 409 on event
  already linked to another meeting; unlink clears both sides; non-fatal reverse-link failure.
- `tests/test_calendar_matcher.py` — `event_uid` populated and its format matches the reader's.
- Pipeline/main — `calendar_event_uid` persisted from a match; reprocess re-supplies it.
- API — `PUT`/`DELETE /calendar-link`: success, 404, 409, 422; WS event emitted.

**UI (vitest):**

- Dedup filter — a linked event is removed from Day/Week/Month/Agenda; unlinked events remain.
- `EventCard` — `⋯` menu + linked annotation render; menu opens the picker.
- `UpcomingEventCard` — "Assign a recording" action appears and opens the picker.
- `CalendarLinkPicker` — lists nearby unlinked candidates, filters, selects.
- Meeting Detail — link/unlink card states + mutation wiring.
- api client wrappers — correct method/URL/body.

## Efficiency

- Manual link = ~2–3 tiny SQL writes, **zero** EventKit reads (picker reuses already-loaded
  query data), **zero** reprocess.
- Dedup is O(n) client-side set filtering.
- Auto-link adds two fields to an existing match — negligible.
- Migration is additive with a safe default; no backfill.

## Risks / cross-branch notes

- **Parallel `feat/calendar-dedup` branch** (commit `6fe011e`, not in this base) dedups
  _calendar entries against each other_ in `reader.py._events_from_extracted`, keeping the
  "richer" copy and thus its `event_uid`. When both branches land, a surviving deduped
  event's `event_uid` could differ from the one a recording auto-linked to, so exact
  `event_uid` collapse might miss. This feature's files do **not** overlap
  (`reader.py` untouched here), so there is no merge conflict; the identity interaction is
  a follow-up concern to verify after both merge. If it proves brittle, the UI dedup can be
  hardened to also match on `teams_meeting_id`/title+time as a fallback — out of scope now.
- `calendar_events` is **not** in `_ALLOWED_TABLES`; we do **not** alter it (only write
  existing columns), so no change needed there.
- Auto-link `event_uid` parity depends on the matcher and reader reading the same EventKit
  identifier and rounding `start_ts` identically (`int(...)`). Covered by a parity test.
