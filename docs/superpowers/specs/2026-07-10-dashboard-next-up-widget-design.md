# Dashboard "Next Up" Widget — Design (Track B, Phase 5)

**Date:** 2026-07-10
**Track:** B — "Calendar hub", Phase 5 (final): a dashboard widget surfacing the user's next upcoming meeting.
**Depends on:** phases 1–4 (calendar import + auto-prep + popover actions + auto-arm). Phases 1–3 are merged to `main`; phase 4 (auto-arm) is PR #53. This phase is **UI-only** and depends only on already-merged foundation APIs (`GET /api/calendar/events`, `GET /api/prep/prepared-events`, `POST /api/prep/by-event/generate`, `POST /api/record/start`), so it does not depend on #53 being merged.

## 1. Problem & context

The Dashboard (`/`, `src/components/dashboard/Dashboard.tsx`) is a column of self-contained widgets (`StatusCard`, `StatsRow`, `PendingCallout`, `HealthSummary`, `OverdueItems`, `RecentMeetings`), each a `useQuery` + `useDaemonStatus` component with loading/error/empty states that renders only when the daemon is running. Upcoming calendar meetings are surfaced today only on the `/calendar` view (via `UpcomingEventCard`'s popover). Nothing on the home dashboard tells the user, at a glance, **what their next meeting is and when** — the natural home for that.

## 2. Goals / non-goals

**Goals:**

- A single **hero card** at the top of the Dashboard showing the user's **next upcoming meeting** (or a meeting **happening now**), with a live countdown.
- Reuse the already-merged calendar/prep/record building blocks; no backend changes.
- Actions on the card: **View/generate prep**, **Join**, **Record now (manual)**, **Open in calendar**.
- Degrade cleanly: daemon offline → nothing; nothing scheduled in 24h → empty state; query error → retry.

**Non-goals:** a new backend endpoint; a multi-event list/agenda (single hero only); snooze/dismiss; a settings toggle; changes to `UpcomingEventCard` or any phase-3 code; recurring-event special handling beyond what the mirror already provides.

## 3. Decisions (from brainstorming)

| Decision            | Choice                                                                                                                                                                            |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Content             | **Single next meeting (hero)**, not a list.                                                                                                                                       |
| Horizon             | **Next 24h**, plus a meeting **in its window now** shown as "Happening now". Empty state ("Nothing scheduled in the next 24h") otherwise.                                         |
| Actions             | **View/generate prep · Join · Record now · Open in calendar** (all four).                                                                                                         |
| Data source         | **Client-side** over `getCalendarEvents(now, now+24h)` — no new endpoint. Pick the earliest event with `end_ts ≥ now`.                                                            |
| Action reuse        | **Approach B** — reuse `PrepModal` + `startRecording`/`generatePrepForEvent` + the `live` gating formula, with the widget's own thin mutation glue. **No edits to phase-3 code.** |
| Placement           | Top of the Dashboard, directly after `StatusCard`.                                                                                                                                |
| Refresh / countdown | Data `refetchInterval: 60s`; a local 1s timer updates the relative-time label.                                                                                                    |

## 4. Architecture

One new self-contained widget, co-located with its dashboard siblings:

```
Dashboard (src/components/dashboard/Dashboard.tsx)
   ├─ StatusCard
   ├─ NextUpWidget  ← NEW (inserted here, after StatusCard)
   ├─ StatsRow
   ├─ PendingCallout
   ├─ HealthSummary
   ├─ OverdueItems
   └─ RecentMeetings

NextUpWidget (src/components/dashboard/NextUpWidget.tsx) — NEW
   • gate: useDaemonStatus().daemonRunning → render null when offline (sibling pattern)
   • data: useQuery(["calendar","next-up"], getCalendarEvents(now..now+24h), refetchInterval 60s)
   •       useQuery(["prepared-events"], getPreparedEventUids)      ← reuses phase-3 query key
   • pick: earliest event with end_ts ≥ now (ordered by start_ts); happeningNow = start_ts ≤ now ≤ end_ts
   • local 1s timer → relative-time label
   • reuses: PrepModal (calendar/PrepModal.tsx), generatePrepForEvent, startRecording, getCalendarEvents,
             getPreparedEventUids, useDaemonStatus, useToast, EmptyState, ErrorState, Skeleton
```

`NextUpWidget` has one clear responsibility (render the next meeting + its actions). It consumes only public API-client functions, the standalone `PrepModal`, and shared common components — no dependency on `UpcomingEventCard` internals.

## 5. Data flow

- **Fetch:** `useQuery({ queryKey: ["calendar","next-up"], queryFn: () => getCalendarEvents(nowSec, nowSec + 86_400), enabled: daemonRunning, refetchInterval: 60_000 })`. `getCalendarEvents(start, end)` already returns `CalendarEvent[]` for the range from the merged live endpoint.
- **Pick:** from the returned events, choose the earliest (`start_ts` ascending) whose `end_ts >= nowSec`. That single event is the hero. `happeningNow = event.start_ts <= nowSec && nowSec <= event.end_ts`.
- **Prepared badge:** `useQuery({ queryKey: ["prepared-events"], queryFn: getPreparedEventUids, enabled: daemonRunning })`; `prepared = uids.has(event.event_uid)`.
- **Countdown:** a local `useEffect` interval (1s) bumps a state counter so the relative label recomputes (`nowSec` read fresh each render). The 60s data refetch rolls the card to the next event once the current one's `end_ts` passes.

## 6. Components & behaviour

**`NextUpWidget` (new).**

- **Gate:** `if (!daemonRunning) return null;`
- **States:** loading → `SkeletonCard`; error → `ErrorState` + `refetch`; no qualifying event → `EmptyState` ("Nothing scheduled in the next 24h"); otherwise the hero.
- **Hero content:**
  - Time line: `happeningNow` → "● Happening now · started {N}m ago"; else a relative label from the minutes-until-start — "in {N} min" when `< 60`, otherwise "in {H}h {M}m" (the 24h horizon means it is never days) — followed by " · {HH:mm}".
  - Title (`event.title || "Untitled"`), "Prep ready" badge when `prepared`.
  - Meta line: attendee count (e.g. "3 attendees"), and when `join_url` is present a provider label derived from the URL host — `teams.` → "Teams", `zoom.` → "Zoom", `meet.google` → "Meet", else "Video call" — joined with " · ". Either part is omitted when empty.
- **Actions (thin wiring reusing merged pieces):**
  - **View / generate prep:** when `prepared`, a "View prep" control opens `PrepModal` (`eventUid`, `title`); a "Generate prep" (or "Regenerate prep") control runs a `generatePrepForEvent` mutation with the **same body shape** `UpcomingEventCard` uses (`{ event_uid, title, attendees, attendee_names: attendees.map(a => a.name || a.email), end_ts, series_id: null }`), and on success `setQueryData(["prep","by-event",event_uid], data)` + `invalidateQueries(["prepared-events"])` + opens `PrepModal`. `onError` → toast.
  - **Join:** `event.join_url` in a new tab (`target="_blank" rel="noreferrer"`); rendered only when `join_url` is non-empty.
  - **Record now:** a `startRecording` mutation gated `live && !isRecording` where `live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts` and `isRecording = useDaemonStatus().state === "recording"`; 2-step inline confirm ("Record this meeting" → "Start recording?" / "Cancel"). Disabled with title "Already recording" when `isRecording` (so an auto-armed recording is reflected), or "Available when the meeting is live" otherwise. `onError` → toast.
  - **Open in calendar:** `navigate("/calendar")`.
- **Reuse:** `PrepModal` is imported from `../calendar/PrepModal` (standalone, unchanged).

**`Dashboard` (modify).** Insert `<NextUpWidget />` between `<StatusCard />` and `<StatsRow />`.

## 7. Error handling

| Condition                           | Behaviour                                                         |
| ----------------------------------- | ----------------------------------------------------------------- |
| Daemon offline                      | `NextUpWidget` renders `null` (sibling pattern)                   |
| Events query loading                | `SkeletonCard`                                                    |
| Events query error                  | `ErrorState` with a retry that calls `refetch`                    |
| No event with `end_ts ≥ now` in 24h | `EmptyState` — "Nothing scheduled in the next 24h"                |
| Event without `join_url`            | Join action + "· Teams/…" hint hidden                             |
| Event without attendees             | attendee line hidden                                              |
| Already recording (incl. auto-arm)  | Record disabled, "Already recording"                              |
| Generate / record request fails     | toast via `useToast` (mutation `onError`); card stays interactive |

## 8. Testing

`src/components/dashboard/__tests__/NextUpWidget.test.tsx` (vitest), modelled on the existing calendar tests: `makeWrapper()` with `QueryClientProvider` + `ToastProvider`, `useDaemonStatus` mocked to `{ daemonRunning: true, state: "idle" }`, and `getCalendarEvents` / `getPreparedEventUids` mocked (via `vi.mock("../../../lib/api", ...)` or a `globalThis.fetch` mock).

Cases:

- Renders the next upcoming event with its title and a relative-time countdown.
- A meeting whose window contains "now" renders as "Happening now".
- No event within 24h → the empty state ("Nothing scheduled in the next 24h").
- `prepared-events` including the event → "Prep ready" badge, and "View prep" opens `PrepModal`.
- Record control disabled when not `live`; disabled with "Already recording" when `state === "recording"`; enabled (and 2-step confirm reachable) when `live && !isRecording`.
- "Generate prep" fires `generatePrepForEvent` with the expected body.

No Python tests (no backend change). `npx tsc --noEmit` clean.

## 9. Roadmap

This is the **final Track B phase**. With it, the calendar hub is complete: import → auto-prep → popover actions → auto-arm recording → dashboard next-up.
