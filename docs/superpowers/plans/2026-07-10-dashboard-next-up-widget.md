# Dashboard "Next Up" Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single hero "Next Up" widget to the top of the Dashboard that surfaces the user's next upcoming meeting (or one happening now) with a live countdown and prep/record/join actions.

**Architecture:** One new self-contained React widget, `NextUpWidget.tsx`, co-located with its dashboard siblings and inserted into `Dashboard.tsx` after `StatusCard`. It follows the sibling pattern (gated on `useDaemonStatus().daemonRunning`, its own `useQuery`s, loading/error/empty/hero states). It reuses already-merged pieces — `getCalendarEvents`, `getPreparedEventUids`, `generatePrepForEvent`, `startRecording`, and the standalone `PrepModal` — with its own thin mutation glue (Approach B). No backend changes.

**Tech Stack:** React 19 + TypeScript, TanStack Query, react-router-dom (`useNavigate`), date-fns, Vitest 4 + React Testing Library, Tailwind.

## Global Constraints

- **UI-only. No backend, no new API endpoint, no migration, no config.**
- **No edits to phase-3 code** (`UpcomingEventCard`, `PrepModal`, calendar components). Reuse `PrepModal` and the API functions as-is.
- **Data source:** client-side over `getCalendarEvents(now, now+86400)`; pick the earliest event with `end_ts >= now` ordered by `start_ts`. `happeningNow = start_ts <= now <= end_ts`.
- **Horizon:** next 24h; `EmptyState` copy exactly "Nothing scheduled" / "in the next 24h" when no qualifying event.
- **Record gating (verbatim from phase-3):** `live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts`; `isRecording = useDaemonStatus().state === "recording"`; button `disabled={!live || isRecording}`; 2-step inline confirm.
- **Generate-prep body (verbatim shape):** `{ event_uid, title, attendees: event.attendees, attendee_names: event.attendees.map(a => a.name || a.email), end_ts: event.end_ts, series_id: null }`; on success `setQueryData(["prep","by-event",event_uid], data)` + `invalidateQueries(["prepared-events"])` then open `PrepModal`; `onError` → toast.
- **Prepared-events query key is exactly `["prepared-events"]`** (shared with phase-3).
- **Placement:** `<NextUpWidget />` between `<StatusCard />` and `<StatsRow />` in `Dashboard.tsx`.
- **Relative-time label:** `mins = round((start - now)/60)`; `< 60` → "in {mins} min"; else "in {h}h {m}m" (drop " {m}m" when `m === 0`). Never days (24h horizon).
- **Provider label from `join_url` (lowercased host contains):** `teams.` → "Teams"; `zoom.` → "Zoom"; `meet.google` → "Meet"; else "Video call".

---

## File Structure

- **Create** `ui/src/components/dashboard/NextUpWidget.tsx` — the widget (one responsibility: render the next meeting + its actions).
- **Modify** `ui/src/components/dashboard/Dashboard.tsx` — insert `<NextUpWidget />` (1 line + 1 import).
- **Create** `ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx` — vitest coverage.

Reused (unchanged): `../../lib/api` (`getCalendarEvents`, `getPreparedEventUids`, `generatePrepForEvent`, `startRecording`), `../../lib/types` (`CalendarEvent`), `../../hooks/useDaemonStatus`, `../common/{EmptyState,ErrorState,Skeleton,Toast}`, `../calendar/PrepModal`, `react-router-dom`, `date-fns`.

**Reference signatures (already in the codebase — do not redefine):**

- `getCalendarEvents(start: number, end: number): Promise<{ events: CalendarEvent[]; count: number }>`
- `getPreparedEventUids(): Promise<{ event_uids: string[] }>`
- `generatePrepForEvent(body: PrepGenerateEventBody): Promise<PrepBriefing>` where `PrepGenerateEventBody = { event_uid: string; title: string; attendees: {name:string;email:string}[]; attendee_names: string[]; end_ts: number; series_id?: string | null }`
- `startRecording(): Promise<RecordingStartResponse>`
- `useDaemonStatus(): { daemonRunning: boolean; state: string; activeMeeting: ... ; isLoading: boolean }`
- `CalendarEvent = { event_uid: string; title: string; start_ts: number; end_ts: number; attendees: {name:string;email:string}[]; organizer; join_url: string; meeting_id: string; calendar_name: string }`
- `PrepModal({ eventUid, title, onClose }: { eventUid: string; title: string; onClose: () => void })`
- `ErrorState({ message?, onRetry? })`, `EmptyState({ title, description, icon? })`, `SkeletonCard()`

---

## Task 1: `NextUpWidget` — data, states, hero display + Dashboard wiring

**Files:**

- Create: `ui/src/components/dashboard/NextUpWidget.tsx`
- Create: `ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx`
- Modify: `ui/src/components/dashboard/Dashboard.tsx`

**Interfaces:**

- Consumes: the reference signatures above.
- Produces: `export function NextUpWidget(): JSX.Element | null` — a dashboard widget. Task 2 adds prep/record actions to the _same_ file.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { NextUpWidget } from "../NextUpWidget";
import { ToastProvider } from "../../common/Toast";
import * as api from "../../../lib/api";
import { useDaemonStatus } from "../../../hooks/useDaemonStatus";

vi.mock("../../../lib/api");
vi.mock("../../../hooks/useDaemonStatus");

const NOW = 1_000_000; // seconds

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function ev(
  overrides: Partial<import("../../../lib/types").CalendarEvent> = {},
) {
  return {
    event_uid: "EK1:1000",
    title: "Weekly Sync",
    start_ts: NOW + 720, // 12 min out
    end_ts: NOW + 720 + 1800,
    attendees: [
      { name: "Sam", email: "sam@x.com" },
      { name: "Kim", email: "kim@x.com" },
    ],
    organizer: null,
    join_url: "https://teams.microsoft.com/l/xyz",
    meeting_id: "",
    calendar_name: "Work",
    ...overrides,
  };
}

beforeEach(() => {
  // Deterministic time WITHOUT fake timers: faking timers would stall
  // Testing Library's async findBy*/waitFor (they poll on setTimeout). The
  // widget's 1s interval uses real timers but never fires within a sub-second
  // test, so no act() warnings.
  vi.spyOn(Date, "now").mockReturnValue(NOW * 1000);
  vi.mocked(useDaemonStatus).mockReturnValue({
    daemonRunning: true,
    state: "idle",
    activeMeeting: null,
    isLoading: false,
  } as ReturnType<typeof useDaemonStatus>);
  vi.mocked(api.getPreparedEventUids).mockResolvedValue({ event_uids: [] });
  vi.mocked(api.getCalendarEvents).mockResolvedValue({
    events: [ev()],
    count: 1,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NextUpWidget", () => {
  it("renders the next upcoming event with a countdown", async () => {
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText("Weekly Sync")).toBeInTheDocument();
    expect(screen.getByText(/in 12 min/)).toBeInTheDocument();
  });

  it("shows 'Happening now' for an in-window event", async () => {
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [ev({ start_ts: NOW - 240, end_ts: NOW + 1800 })],
      count: 1,
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Happening now/i)).toBeInTheDocument();
  });

  it("shows the empty state when nothing is in the next 24h", async () => {
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [],
      count: 0,
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Nothing scheduled/i)).toBeInTheDocument();
  });

  it("renders nothing when the daemon is offline", () => {
    vi.mocked(useDaemonStatus).mockReturnValue({
      daemonRunning: false,
      state: "unknown",
      activeMeeting: null,
      isLoading: false,
    } as ReturnType<typeof useDaemonStatus>);
    const { container } = render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a 'Prep ready' badge when the event is prepared", async () => {
    vi.mocked(api.getPreparedEventUids).mockResolvedValue({
      event_uids: ["EK1:1000"],
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Prep ready/i)).toBeInTheDocument();
  });

  it("shows a Join link when the event has a join_url", async () => {
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    const join = await screen.findByRole("link", { name: /join/i });
    expect(join).toHaveAttribute("href", "https://teams.microsoft.com/l/xyz");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ui && npm test -- NextUpWidget`
Expected: FAIL — cannot resolve `../NextUpWidget`.

- [ ] **Step 3: Write the widget (display only — no prep/record buttons yet)**

Create `ui/src/components/dashboard/NextUpWidget.tsx`:

```tsx
import { useEffect, useState } from "react";
import { format } from "date-fns";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent } from "../../lib/types";
import { getCalendarEvents, getPreparedEventUids } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";

const DAY_SECONDS = 86_400;

function providerLabel(joinUrl: string): string {
  const u = joinUrl.toLowerCase();
  if (u.includes("teams.")) return "Teams";
  if (u.includes("zoom.")) return "Zoom";
  if (u.includes("meet.google")) return "Meet";
  return "Video call";
}

function relativeLabel(startSec: number, nowSec: number): string {
  const mins = Math.max(0, Math.round((startSec - nowSec) / 60));
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `in ${h}h ${m}m` : `in ${h}h`;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-surface-raised border border-border p-6">
      <h2 className="text-sm font-medium text-text-primary mb-4">Next up</h2>
      {children}
    </div>
  );
}

export function NextUpWidget() {
  const { daemonRunning, state } = useDaemonStatus();
  const navigate = useNavigate();

  // Re-render every second so the relative countdown stays live.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const nowSec = Date.now() / 1000;

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["calendar", "next-up"],
    queryFn: () =>
      getCalendarEvents(Math.floor(nowSec), Math.floor(nowSec) + DAY_SECONDS),
    enabled: daemonRunning,
    refetchInterval: 60_000,
  });

  const { data: preparedData } = useQuery({
    queryKey: ["prepared-events"],
    queryFn: getPreparedEventUids,
    enabled: daemonRunning,
  });

  if (!daemonRunning) return null;

  if (isLoading) {
    return (
      <Shell>
        <div role="status" aria-label="Loading next meeting">
          <SkeletonCard />
        </div>
      </Shell>
    );
  }

  if (isError) {
    return (
      <Shell>
        <ErrorState
          message="Failed to load your calendar."
          onRetry={() => refetch()}
        />
      </Shell>
    );
  }

  const event: CalendarEvent | undefined = (data?.events ?? [])
    .filter((e) => e.end_ts >= nowSec)
    .sort((a, b) => a.start_ts - b.start_ts)[0];

  if (!event) {
    return (
      <Shell>
        <EmptyState title="Nothing scheduled" description="in the next 24h" />
      </Shell>
    );
  }

  const happeningNow = event.start_ts <= nowSec && nowSec <= event.end_ts;
  const prepared = new Set(preparedData?.event_uids ?? []).has(event.event_uid);
  const title = event.title || "Untitled";
  const startedMins = Math.max(0, Math.round((nowSec - event.start_ts) / 60));

  const metaParts: string[] = [];
  if (event.attendees.length > 0) {
    metaParts.push(
      `${event.attendees.length} attendee${event.attendees.length === 1 ? "" : "s"}`,
    );
  }
  if (event.join_url) metaParts.push(providerLabel(event.join_url));

  return (
    <Shell>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-xs">
          {happeningNow ? (
            <span className="flex items-center gap-1.5 text-status-recording">
              <span className="w-2 h-2 rounded-full bg-status-recording animate-pulse" />
              Happening now
              <span className="text-text-muted">
                · started {startedMins}m ago
              </span>
            </span>
          ) : (
            <span className="text-accent">
              {relativeLabel(event.start_ts, nowSec)}
              <span className="text-text-muted">
                {" "}
                · {format(new Date(event.start_ts * 1000), "HH:mm")}
              </span>
            </span>
          )}
          {prepared && (
            <span className="ml-auto rounded bg-accent/20 text-accent px-1.5 py-0.5 text-[10px]">
              Prep ready
            </span>
          )}
        </div>

        <p className="text-base font-medium text-text-primary">{title}</p>

        {metaParts.length > 0 && (
          <p className="text-xs text-text-muted">{metaParts.join(" · ")}</p>
        )}

        <div className="mt-1 flex items-center gap-3 text-xs">
          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Join
            </a>
          )}
          <button
            type="button"
            onClick={() => navigate("/calendar")}
            className="text-text-muted hover:text-text-secondary"
          >
            Open in calendar
          </button>
        </div>
      </div>
    </Shell>
  );
}
```

Note: `state` is destructured now (used by Task 2's record gating); referencing it keeps the import stable. If your linter flags it as unused in this task, prefix with `void state;` after the `useNavigate` line and remove that line in Task 2.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ui && npm test -- NextUpWidget`
Expected: PASS (6 tests).

- [ ] **Step 5: Wire it into the Dashboard**

In `ui/src/components/dashboard/Dashboard.tsx`, add the import beside the other dashboard-component imports (next to `import { HealthSummary } from "./HealthSummary";`):

```tsx
import { NextUpWidget } from "./NextUpWidget";
```

Then in the `Dashboard` function's returned JSX, insert `<NextUpWidget />` between `<StatusCard />` and `<StatsRow />`:

```tsx
      <StatusCard />
      <NextUpWidget />
      <StatsRow />
```

- [ ] **Step 6: Type-check and run the full UI suite**

Run: `cd ui && npx tsc --noEmit && npm test`
Expected: no type errors; all tests pass (existing + 6 new).

- [ ] **Step 7: Commit**

```bash
git add ui/src/components/dashboard/NextUpWidget.tsx ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx ui/src/components/dashboard/Dashboard.tsx
git commit -m "feat(ui): dashboard Next Up widget (display + wiring)"
```

---

## Task 2: prep + record actions

**Files:**

- Modify: `ui/src/components/dashboard/NextUpWidget.tsx`
- Modify: `ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx`

**Interfaces:**

- Consumes: `NextUpWidget` from Task 1 (same file); `generatePrepForEvent`, `startRecording` (reference signatures above); `PrepModal({ eventUid, title, onClose })`; `useToast()`.
- Produces: the finished widget with View/Generate prep (+ `PrepModal`), and Record-now (gated, 2-step confirm).

- [ ] **Step 1: Write the failing tests (append to the existing test file)**

Add these cases inside the `describe("NextUpWidget", ...)` block in `ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx`. Also add `fireEvent` to the testing-library import (`import { render, screen, fireEvent } from "@testing-library/react";`):

```tsx
it("opens the prep modal when 'View prep' is clicked (prepared event)", async () => {
  vi.mocked(api.getPreparedEventUids).mockResolvedValue({
    event_uids: ["EK1:1000"],
  });
  vi.mocked(api.getPrepByEvent).mockResolvedValue(null);
  render(<NextUpWidget />, { wrapper: makeWrapper() });

  const viewPrep = await screen.findByRole("button", { name: /view prep/i });
  fireEvent.click(viewPrep);

  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});

it("fires generatePrepForEvent with the expected body", async () => {
  vi.mocked(api.getPrepByEvent).mockResolvedValue(null);
  vi.mocked(api.generatePrepForEvent).mockResolvedValue({
    id: "p1",
  } as Awaited<ReturnType<typeof api.generatePrepForEvent>>);
  render(<NextUpWidget />, { wrapper: makeWrapper() });

  const gen = await screen.findByRole("button", { name: /generate prep/i });
  fireEvent.click(gen);

  expect(api.generatePrepForEvent).toHaveBeenCalledWith(
    expect.objectContaining({
      event_uid: "EK1:1000",
      title: "Weekly Sync",
      attendee_names: ["Sam", "Kim"],
      end_ts: NOW + 720 + 1800,
      series_id: null,
    }),
  );
});

it("disables Record when the event is not live", async () => {
  // start_ts 12 min out (> 5 min) → not live yet.
  render(<NextUpWidget />, { wrapper: makeWrapper() });
  const rec = await screen.findByRole("button", {
    name: /record this meeting/i,
  });
  expect(rec).toBeDisabled();
});

it("disables Record with 'Already recording' when the daemon is recording", async () => {
  vi.mocked(useDaemonStatus).mockReturnValue({
    daemonRunning: true,
    state: "recording",
    activeMeeting: null,
    isLoading: false,
  } as ReturnType<typeof useDaemonStatus>);
  vi.mocked(api.getCalendarEvents).mockResolvedValue({
    events: [ev({ start_ts: NOW - 60, end_ts: NOW + 1800 })], // live window
    count: 1,
  });
  render(<NextUpWidget />, { wrapper: makeWrapper() });
  const rec = await screen.findByRole("button", {
    name: /record this meeting/i,
  });
  expect(rec).toBeDisabled();
  expect(rec).toHaveAttribute("title", "Already recording");
});

it("records via a 2-step confirm when the event is live", async () => {
  vi.mocked(api.getCalendarEvents).mockResolvedValue({
    events: [ev({ start_ts: NOW - 60, end_ts: NOW + 1800 })], // live, not recording
    count: 1,
  });
  vi.mocked(api.startRecording).mockResolvedValue(
    {} as Awaited<ReturnType<typeof api.startRecording>>,
  );
  render(<NextUpWidget />, { wrapper: makeWrapper() });

  fireEvent.click(
    await screen.findByRole("button", { name: /record this meeting/i }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: /start recording\?/i }),
  );
  expect(api.startRecording).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ui && npm test -- NextUpWidget`
Expected: FAIL — no "View prep"/"Generate prep"/"Record this meeting" controls yet.

- [ ] **Step 3: Add the prep + record actions to `NextUpWidget.tsx`**

Update the imports at the top of `ui/src/components/dashboard/NextUpWidget.tsx`:

```tsx
import { useEffect, useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent } from "../../lib/types";
import {
  getCalendarEvents,
  getPreparedEventUids,
  generatePrepForEvent,
  startRecording,
} from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useToast } from "../common/Toast";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { PrepModal } from "../calendar/PrepModal";
```

Inside `NextUpWidget`, add these hooks right after the `useNavigate()` line (and remove the `void state;` line if you added it in Task 1):

```tsx
const queryClient = useQueryClient();
const toast = useToast();
const [showPrep, setShowPrep] = useState(false);
const [confirmingRecord, setConfirmingRecord] = useState(false);
```

After the `event` / `happeningNow` / `prepared` / `title` computations (and before the `return`), add the derived gating flags and the two mutations. `event` is defined and non-undefined at this point (the `if (!event) return ...` guard is above):

```tsx
const isRecording = state === "recording";
const live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts;

const generate = useMutation({
  mutationFn: () =>
    generatePrepForEvent({
      event_uid: event.event_uid,
      title,
      attendees: event.attendees,
      attendee_names: event.attendees.map((a) => a.name || a.email),
      end_ts: event.end_ts,
      series_id: null,
    }),
  onSuccess: (dataPrep) => {
    queryClient.setQueryData(["prep", "by-event", event.event_uid], dataPrep);
    void queryClient.invalidateQueries({ queryKey: ["prepared-events"] });
    setShowPrep(true);
  },
  onError: () => toast.error("Failed to generate prep."),
});

const record = useMutation({
  mutationFn: () => startRecording(),
  onSuccess: () => setConfirmingRecord(false),
  onError: () => toast.error("Failed to start recording."),
});
```

> **Hooks rule:** `useMutation`/`useQueryClient`/`useToast`/`useState` must run on every render in the same order. The early returns for `!daemonRunning` / loading / error / `!event` sit **above** these hooks, so they are unconditional relative to each other — keep all hook calls before the first early `return`. Move `useQueryClient`, `useToast`, and the two `useState`s up next to the other top-level hooks (after `useNavigate`), and compute `isRecording`/`live`/the mutations only after `event` is known — mutations may be declared after the guards **only if** every render that reaches them has already run the same hooks in the same order. To stay unambiguously correct: declare `useMutation` for `generate` and `record` at the top level too, but reference `event` through a captured local that is guaranteed defined. Simplest safe structure: keep ALL `use*` calls (including both `useMutation`s) above every early `return`, deriving `event` first with `useMemo`. See Step 3b for the exact final file.

- [ ] **Step 3b: Use this exact final `NextUpWidget.tsx` (hooks-safe ordering)**

Replace the entire file with:

```tsx
import { useEffect, useMemo, useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent } from "../../lib/types";
import {
  getCalendarEvents,
  getPreparedEventUids,
  generatePrepForEvent,
  startRecording,
} from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useToast } from "../common/Toast";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { PrepModal } from "../calendar/PrepModal";

const DAY_SECONDS = 86_400;

function providerLabel(joinUrl: string): string {
  const u = joinUrl.toLowerCase();
  if (u.includes("teams.")) return "Teams";
  if (u.includes("zoom.")) return "Zoom";
  if (u.includes("meet.google")) return "Meet";
  return "Video call";
}

function relativeLabel(startSec: number, nowSec: number): string {
  const mins = Math.max(0, Math.round((startSec - nowSec) / 60));
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `in ${h}h ${m}m` : `in ${h}h`;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-surface-raised border border-border p-6">
      <h2 className="text-sm font-medium text-text-primary mb-4">Next up</h2>
      {children}
    </div>
  );
}

export function NextUpWidget() {
  const { daemonRunning, state } = useDaemonStatus();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();

  const [, setTick] = useState(0);
  const [showPrep, setShowPrep] = useState(false);
  const [confirmingRecord, setConfirmingRecord] = useState(false);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const nowSec = Date.now() / 1000;

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["calendar", "next-up"],
    queryFn: () =>
      getCalendarEvents(Math.floor(nowSec), Math.floor(nowSec) + DAY_SECONDS),
    enabled: daemonRunning,
    refetchInterval: 60_000,
  });

  const { data: preparedData } = useQuery({
    queryKey: ["prepared-events"],
    queryFn: getPreparedEventUids,
    enabled: daemonRunning,
  });

  const event: CalendarEvent | undefined = useMemo(
    () =>
      (data?.events ?? [])
        .filter((e) => e.end_ts >= nowSec)
        .sort((a, b) => a.start_ts - b.start_ts)[0],
    [data, nowSec],
  );

  const generate = useMutation({
    mutationFn: () => {
      if (!event) throw new Error("no event");
      return generatePrepForEvent({
        event_uid: event.event_uid,
        title: event.title || "Untitled",
        attendees: event.attendees,
        attendee_names: event.attendees.map((a) => a.name || a.email),
        end_ts: event.end_ts,
        series_id: null,
      });
    },
    onSuccess: (dataPrep) => {
      if (!event) return;
      queryClient.setQueryData(["prep", "by-event", event.event_uid], dataPrep);
      void queryClient.invalidateQueries({ queryKey: ["prepared-events"] });
      setShowPrep(true);
    },
    onError: () => toast.error("Failed to generate prep."),
  });

  const record = useMutation({
    mutationFn: () => startRecording(),
    onSuccess: () => setConfirmingRecord(false),
    onError: () => toast.error("Failed to start recording."),
  });

  if (!daemonRunning) return null;

  if (isLoading) {
    return (
      <Shell>
        <div role="status" aria-label="Loading next meeting">
          <SkeletonCard />
        </div>
      </Shell>
    );
  }

  if (isError) {
    return (
      <Shell>
        <ErrorState
          message="Failed to load your calendar."
          onRetry={() => refetch()}
        />
      </Shell>
    );
  }

  if (!event) {
    return (
      <Shell>
        <EmptyState title="Nothing scheduled" description="in the next 24h" />
      </Shell>
    );
  }

  const happeningNow = event.start_ts <= nowSec && nowSec <= event.end_ts;
  const prepared = new Set(preparedData?.event_uids ?? []).has(event.event_uid);
  const title = event.title || "Untitled";
  const startedMins = Math.max(0, Math.round((nowSec - event.start_ts) / 60));
  const isRecording = state === "recording";
  const live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts;

  const metaParts: string[] = [];
  if (event.attendees.length > 0) {
    metaParts.push(
      `${event.attendees.length} attendee${event.attendees.length === 1 ? "" : "s"}`,
    );
  }
  if (event.join_url) metaParts.push(providerLabel(event.join_url));

  return (
    <Shell>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-xs">
          {happeningNow ? (
            <span className="flex items-center gap-1.5 text-status-recording">
              <span className="w-2 h-2 rounded-full bg-status-recording animate-pulse" />
              Happening now
              <span className="text-text-muted">
                · started {startedMins}m ago
              </span>
            </span>
          ) : (
            <span className="text-accent">
              {relativeLabel(event.start_ts, nowSec)}
              <span className="text-text-muted">
                {" "}
                · {format(new Date(event.start_ts * 1000), "HH:mm")}
              </span>
            </span>
          )}
          {prepared && (
            <span className="ml-auto rounded bg-accent/20 text-accent px-1.5 py-0.5 text-[10px]">
              Prep ready
            </span>
          )}
        </div>

        <p className="text-base font-medium text-text-primary">{title}</p>

        {metaParts.length > 0 && (
          <p className="text-xs text-text-muted">{metaParts.join(" · ")}</p>
        )}

        <div className="mt-1 flex flex-wrap items-center gap-3 text-xs border-t border-border pt-3">
          {prepared && (
            <button
              type="button"
              onClick={() => setShowPrep(true)}
              className="text-accent hover:underline"
            >
              View prep
            </button>
          )}
          <button
            type="button"
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            className="text-accent hover:underline disabled:opacity-50"
          >
            {generate.isPending
              ? "Generating..."
              : prepared
                ? "Regenerate prep"
                : "Generate prep"}
          </button>

          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Join
            </a>
          )}

          {!confirmingRecord ? (
            <button
              type="button"
              onClick={() => setConfirmingRecord(true)}
              disabled={!live || isRecording}
              title={
                isRecording
                  ? "Already recording"
                  : live
                    ? ""
                    : "Available when the meeting is live"
              }
              className="text-accent hover:underline disabled:opacity-40 disabled:no-underline disabled:text-text-muted"
            >
              Record this meeting
            </button>
          ) : (
            <span className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => record.mutate()}
                disabled={record.isPending}
                className="text-accent hover:underline disabled:opacity-50"
              >
                Start recording?
              </button>
              <button
                type="button"
                onClick={() => setConfirmingRecord(false)}
                className="text-text-muted hover:text-text-secondary"
              >
                Cancel
              </button>
            </span>
          )}

          <button
            type="button"
            onClick={() => navigate("/calendar")}
            className="ml-auto text-text-muted hover:text-text-secondary"
          >
            Open in calendar
          </button>
        </div>
      </div>

      {showPrep && (
        <PrepModal
          eventUid={event.event_uid}
          title={title}
          onClose={() => setShowPrep(false)}
        />
      )}
    </Shell>
  );
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ui && npm test -- NextUpWidget`
Expected: PASS (all 11 tests — 6 from Task 1 + 5 from Task 2).

- [ ] **Step 5: Type-check + full UI suite**

Run: `cd ui && npx tsc --noEmit && npm test`
Expected: no type errors; all tests pass.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/dashboard/NextUpWidget.tsx ui/src/components/dashboard/__tests__/NextUpWidget.test.tsx
git commit -m "feat(ui): Next Up widget prep + record actions"
```

---

## Final Validation

- [ ] `cd ui && npm test` — full vitest suite green.
- [ ] `cd ui && npx tsc --noEmit` — clean.

(No Python tests — this phase makes no backend change.)

---

## Self-Review Notes (author)

- **Spec coverage:** hero single-event (Task 1 pick + display); next-24h horizon + "Happening now" + empty copy (Task 1); prepared badge (Task 1); View/generate prep + `PrepModal` (Task 2); Join (Task 1); Record-now gated + 2-step confirm (Task 2); Open in calendar (Task 1); placement after `StatusCard` (Task 1 Step 5); live countdown (Task 1 timer); loading/error/empty (Task 1); testing table (both tasks). No backend/migration/config — honored.
- **Deviation:** the widget declares all hooks (incl. both `useMutation`s) above the early `return`s and derives `event` via `useMemo`, so the Rules of Hooks hold despite the `!event` guard. Step 3b is the authoritative final file (Step 3's prose is context; the implementer should land Step 3b's exact content).
- **Type consistency:** `generatePrepForEvent` body matches `PrepGenerateEventBody`; `getCalendarEvents` returns `{events,count}`; `getPreparedEventUids` returns `{event_uids}`; `PrepModal` props `{eventUid,title,onClose}`; `useDaemonStatus` fields `{daemonRunning,state}` — all verified against the codebase.
- **Test seams:** `vi.mock("../../../lib/api")` + `vi.mock("../../../hooks/useDaemonStatus")` + `MemoryRouter`; deterministic time via `vi.spyOn(Date, "now")` (NOT fake timers — those would stall Testing Library's async `findBy*`). `getPrepByEvent` is mocked in the prep-modal test because `PrepModal` calls it on open.
