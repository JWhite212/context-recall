# Event-Popover Prep/Record Actions — Design (Track B, Phase 3)

**Date:** 2026-07-10
**Track:** B — "Calendar hub", Phase 3 (event-popover prep/record actions)
**Depends on:** phases 1 (calendar import) + 2 (auto-prep briefings), **both merged to `main`**. This phase branches off `main`.

## 1. Problem & context

Phase 2 gave upcoming calendar events a read-only affordance: `UpcomingEventCard` shows a "Prep ready" badge and an info popover (title / time / attendees / Join link), and auto-generated briefings appear in the Prep view list. But the popover is **inert** — you can't open a briefing from it, generate prep on demand for an event the sweep skipped, or start recording a meeting you're about to join.

Two gaps make it inert:

- **Prep is not addressable by event.** Viewing/generating prep is keyed by _recorded_ `meeting_id` (`GET /api/prep/{meeting_id}`, `POST /api/prep/{meeting_id}/generate`). There is no way to fetch or generate a briefing by `calendar_event_uid`.
- **No record entry point on events.** Recording is a global `POST /api/record/start` ("record now"); nothing surfaces it from a calendar event.

Phase 3 makes the popover interactive: **view / generate / regenerate prep** for the event, and **record this meeting** when it's live.

## 2. Goals / non-goals

**Goals:**

- Address prep by `calendar_event_uid`: `GET /api/prep/by-event/{uid}` and a generate-on-demand endpoint.
- Interactive `UpcomingEventCard` popover: View prep (modal), Generate/Regenerate, Record this meeting.
- A `PrepModal` that renders a briefing over the calendar.
- A gated, confirmed "Record this meeting" action reusing the existing recording endpoint.

**Non-goals:** auto-arm/auto-start recording (its own later phase); the dashboard "next up" widget; any new DB schema or config (this phase adds none).

## 3. Decisions (from brainstorming)

| Decision             | Choice                                                                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Action set           | **Prep + Record** — view/generate/regenerate prep, and "Record this meeting".                                                                                                                 |
| Prep view UX         | **Modal over the calendar** (rendered markdown), not navigation or inline-expand.                                                                                                             |
| Record availability  | **Only when live/imminent** (`start_ts − 300s ≤ now ≤ end_ts`) **and** not already recording; **two-step inline confirm**; the pipeline auto-links the recording to the event by time-window. |
| Generate semantics   | Manual generate **always** produces a briefing (bypasses the sweep's context-rich filter — that filter lives in `PrepSweep`, not `generate()`).                                               |
| Generate data source | **UI sends the event in the request body** (works for any shown event, incl. live-read events outside the mirror window) — not a `calendar_events` lookup.                                    |

## 4. Architecture

```
Backend (extend prep routes; reuse recording route)
   ├─ GET  /api/prep/by-event/{event_uid}   → current briefing for the event
   │        (PrepRepository.get_by_calendar_event — added in phase 2)
   │        [2 path segments; does NOT collide with GET /{meeting_id} (1 segment) — order-independent]
   ├─ POST /api/prep/by-event/generate      → generate/regenerate on demand
   │        MUST be declared BEFORE POST /{meeting_id}/generate (both 2-segment ".../generate"
   │        → /{meeting_id}/generate would otherwise capture it with meeting_id="by-event")
   │        body: {event_uid, title, attendees[], attendee_names[], end_ts, series_id?}
   │        server computes event_signature via src.prep.sweep.event_signature; then
   │        PrepBriefingGenerator.generate(..., calendar_event_uid, event_signature, expires_at=end_ts)
   └─ POST /api/record/start                 → EXISTING, reused for "Record this meeting"

Frontend (calendar)
   ├─ UpcomingEventCard popover → action row:
   │     • View prep (when prepared) → opens PrepModal
   │     • Generate / Regenerate → POST by-event/generate → invalidate queries → open PrepModal
   │     • Record this meeting → gated (live && !isRecording) → 2-step confirm → startRecording()
   ├─ PrepModal (NEW) → GET /api/prep/by-event/{uid}, renders markdown, close + backdrop
   └─ lib/api: getPrepByEvent(uid), generatePrepForEvent(body); startRecording() reused
```

**Data flow.**

- _View:_ popover → `PrepModal` → `GET /api/prep/by-event/{uid}` → render markdown over the calendar.
- _Generate:_ popover → `POST /api/prep/by-event/generate` (event in body) → briefing linked to `calendar_event_uid` → invalidate `["prepared-events"]` + `["prep","by-event",uid]` (badge flips to "Prep ready") → open the modal.
- _Record:_ the card computes `live = (start_ts − 300) ≤ nowSec ≤ end_ts`; the button is enabled only when `live && !isRecording` (recording state from the existing `usePipelineSync` hook); a two-step inline confirm → `startRecording()`. The running pipeline's `CalendarMatcher` links the recording to the event by time-window — no new linkage code.

## 5. Backend behaviour

**`GET /api/prep/by-event/{event_uid}`** — a 2-segment path (`by-event/{uid}`), so it does **not** collide with the 1-segment `GET /{meeting_id}` and is order-independent. Returns `_get_repo().get_by_calendar_event(event_uid)`; **204 No Content** when none (mirrors the existing `/upcoming` pattern, so the UI shows "Generate" vs "View"). Repo unset → 503.

**`POST /api/prep/by-event/generate`** — **MUST be declared before `POST /{meeting_id}/generate`** (both are 2-segment `.../generate`, so `/{meeting_id}/generate` would otherwise match this request with `meeting_id="by-event"`). (201) — Pydantic body `{event_uid: str (min_length 1), title: str, attendees: list[Attendee] ({name, email}), attendee_names: list[str], end_ts: float, series_id: str | None}`. The server derives `emails = [a.email for a in attendees if a.email]`, `event_signature = src.prep.sweep.event_signature(emails)`, then calls the already-wired `PrepBriefingGenerator.generate(title=body.title, attendees=emails, attendee_names=body.attendee_names, series_id=body.series_id, calendar_event_uid=body.event_uid, event_signature=sig, expires_at=body.end_ts)` and returns `get_by_calendar_event(body.event_uid)`. Generator unset → 503; invalid body → 422; an LLM failure still yields a fallback briefing (phase-2 behaviour). This path naturally bypasses the context-rich filter (it lives in `PrepSweep`, not `generate()`).

**Record** — reuses `POST /api/record/start` unchanged. If a recording is already in progress the existing route handles it; the UI also gates on `!isRecording`.

**Regenerate** creates a new `prep_briefings` row for the same `calendar_event_uid`; `get_by_calendar_event` returns the newest and the old row expires at its `end_ts` (the same benign lingering as the phase-2 `list_upcoming` minor; the standing de-dupe fast-follow covers both).

No `CalendarEventRepository` change and **no new migration/config** — the event comes from the request body; the GET reads `prep_briefings`.

## 6. Error handling

| Condition                            | Behaviour                                                                     |
| ------------------------------------ | ----------------------------------------------------------------------------- |
| `by-event` GET, no briefing          | 204 → UI shows "Generate"                                                     |
| prep repo / generator unset          | 503                                                                           |
| invalid generate body                | 422                                                                           |
| LLM failure during generate          | fallback briefing still created + linked (phase-2 behaviour)                  |
| record start while already recording | existing route's behaviour; UI's Record button is disabled when `isRecording` |
| record on a non-live event           | button disabled + hint; endpoint never called                                 |

## 7. Frontend

- **`lib/api.ts`** — `getPrepByEvent(uid)` (raw request, 204 → `null`), `generatePrepForEvent(body)`; `startRecording()` already exists (reused).
- **`lib/types.ts`** — a `PrepGenerateEventBody` type (or inline object type on the client fn).
- **`PrepModal` (NEW, `ui/src/components/calendar/PrepModal.tsx`)** — `{ eventUid, onClose }`; `useQuery(["prep","by-event",uid], () => getPrepByEvent(uid))`; renders the markdown via `react-markdown` in the existing `prose prose-sm prose-invert ...` wrapper; loading skeleton; close button + backdrop overlay.
- **`UpcomingEventCard`** popover — an action row:
  - **View prep** (when `prepared`) → open `PrepModal`.
  - **Generate prep** / **Regenerate** → `useMutation(generatePrepForEvent)` → on success invalidate `["prepared-events"]` + `["prep","by-event",uid]` → open the modal.
  - **Record this meeting** → `live = (event.start_ts − 300) ≤ nowSec ≤ event.end_ts`; enabled only when `live && !isRecording` (from `usePipelineSync`); **two-step inline confirm** (button → "Start recording?" → confirm) → `startRecording()`; disabled + hint ("Available when the meeting is live") otherwise.

## 8. Testing

**Python** — extend `tests/test_api_prep.py`: `by-event` GET (204 when none / the briefing when present, non-404 proving it precedes `/{meeting_id}`); `by-event/generate` (201, creates + links to `calendar_event_uid`; a second call regenerates and `get_by_calendar_event` returns the newest). The generate route is tested with a `PrepBriefingGenerator` whose `_summariser.chat` is stubbed (no real model), as in phase 2.

**UI (vitest)** — `PrepModal` renders briefing markdown from a mocked GET; `UpcomingEventCard` shows **Generate** when not prepared / **View** when prepared, clicking Generate POSTs to `by-event/generate` and opens the modal, and the **Record** button is disabled when not live or already recording but enabled + confirm-then-`startRecording()` when live.

## 9. Roadmap — remaining phases

Unchanged; each its own spec→plan→build:

1. **Auto-arm / auto-start recording** for scheduled calendar meetings.
2. **Dashboard "next up" widget.**
