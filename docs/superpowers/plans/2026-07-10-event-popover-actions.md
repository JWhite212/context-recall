# Event-Popover Prep/Record Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the calendar `UpcomingEventCard` popover interactive — view a briefing in a modal, generate/regenerate prep on demand, and record a live meeting — via new by-event prep endpoints and a reused recording endpoint.

**Architecture:** Two new prep routes address briefings by `calendar_event_uid` (`GET /api/prep/by-event/{uid}`, `POST /api/prep/by-event/generate` with the event in the body), reusing phase-2's wired `PrepBriefingGenerator`. The React popover gains action buttons, a new `PrepModal` renders markdown over the calendar, and the "Record this meeting" button reuses `startRecording()` gated on the event being live and the daemon not already recording.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, pytest + pytest-asyncio; React 19 + TypeScript, TanStack Query, react-markdown, Vitest 4, date-fns.

## Global Constraints

- **Branch:** work on `feat/calendar-popover-actions` (off `main`). **No new DB migration and no new config** — this phase adds neither.
- **Route ordering (prep.py, `prefix="/api/prep"`):** `POST /api/prep/by-event/generate` MUST be declared **before** `POST /{meeting_id}/generate` (both are 2-segment `.../generate`, so `/{meeting_id}/generate` would otherwise capture it with `meeting_id="by-event"`). `GET /api/prep/by-event/{uid}` is 2-segment and does not collide with the 1-segment `GET /{meeting_id}` (order-independent) — but place both new routes together, right after `@router.get("/prepared-events")` and before `@router.get("/{meeting_id}")`.
- **Generate is unconditional:** `POST by-event/generate` calls `PrepBriefingGenerator.generate(...)` directly (the context-rich filter lives in `PrepSweep`, not `generate()`), so a manual generate always produces a briefing. `expires_at` = the event's `end_ts`.
- **`event_signature`** is computed server-side via `from src.prep.sweep import event_signature` over the event's non-empty attendee emails.
- **Record gating (UI):** the "Record this meeting" button is enabled only when `live && !isRecording`, where `live = (event.start_ts - 300) <= Date.now()/1000 <= event.end_ts` and `isRecording = useDaemonStatus().state === "recording"`. It uses a **two-step inline confirm** (no JS dialog) and reuses `startRecording()`. The pipeline's `CalendarMatcher` links the recording to the event by time-window — no new linkage.
- **Prep view UX:** briefings open in a `PrepModal` overlaying the calendar (react-markdown in the `prose prose-sm prose-invert ...` wrapper used elsewhere), not navigation/inline-expand.
- **Reused, unchanged:** `PrepRepository.get_by_calendar_event(uid)`, `PrepBriefingGenerator.generate(..., calendar_event_uid, event_signature, expires_at)`, `startRecording()`, `RecordingStartResponse`.
- **Tests:** API tests `@pytest.mark.asyncio`, auth via `src.api.auth._auth_token` monkeypatch, minimal FastAPI app + TestClient, LLM stubbed via `generator._summariser.chat = ...`. UI tests via Vitest + Testing Library; mock `useDaemonStatus` and `globalThis.fetch`.
- **Commands:** Python `python3 -m pytest <path> -v`, `ruff check src/ tests/`. UI `cd ui && npm test`, `cd ui && npx tsc --noEmit`. Use `source .venv/bin/activate` if `.venv` exists.

---

### Task 1: Backend — prep by-event routes

**Files:**

- Modify: `src/api/routes/prep.py`
- Test: `tests/test_api_prep.py`

**Interfaces:**

- Consumes: `PrepRepository.get_by_calendar_event(uid) -> dict | None`; `PrepBriefingGenerator.generate(title, attendees, attendee_names, series_id=None, meeting_id=None, calendar_event_uid=None, event_signature=None, expires_at=None) -> str`; `src.prep.sweep.event_signature(emails: list[str]) -> str`.
- Produces: `GET /api/prep/by-event/{event_uid}` (204 when none); `POST /api/prep/by-event/generate` (201) with body `{event_uid, title, attendees:[{name,email}], attendee_names:[str], end_ts, series_id?}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_prep.py` (or extend if present) — this adds a generator to the app fixture and tests the two new routes:

```python
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import prep as prep_routes
from src.action_items.repository import ActionItemRepository
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository
from src.series.repository import SeriesRepository
from src.utils.config import PrepConfig, SummarisationConfig

TEST_TOKEN = "test-token-for-prep-events"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "prep_evt_api.db")
    await db.connect()
    repo = PrepRepository(db)
    gen = PrepBriefingGenerator(
        config=PrepConfig(),
        summarisation_config=SummarisationConfig(),
        meeting_repo=MeetingRepository(db),
        action_item_repo=ActionItemRepository(db),
        series_repo=SeriesRepository(db),
        prep_repo=repo,
    )
    gen._summariser.chat = lambda system, user: "## Prep\nstub briefing"
    prep_routes.init(repo, gen)
    app = FastAPI()
    app.include_router(prep_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo}
    await db.close()


def _body(uid="EK1:1000"):
    return {
        "event_uid": uid,
        "title": "Weekly sync",
        "attendees": [{"name": "Alice", "email": "a@x.com"}],
        "attendee_names": ["Alice"],
        "end_ts": time.time() + 3600,
        "series_id": None,
    }


@pytest.mark.asyncio
async def test_by_event_get_204_when_none(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/by-event/NOPE:0", headers=_headers())
        assert r.status_code == 204


@pytest.mark.asyncio
async def test_by_event_generate_then_get(api):
    with TestClient(api["app"]) as c:
        gen = c.post("/api/prep/by-event/generate", headers=_headers(), json=_body())
        assert gen.status_code == 201
        assert gen.json()["calendar_event_uid"] == "EK1:1000"
        assert "stub briefing" in gen.json()["content_markdown"]
        got = c.get("/api/prep/by-event/EK1:1000", headers=_headers())
        assert got.status_code == 200
        assert got.json()["event_signature"]  # signature was computed + stored


@pytest.mark.asyncio
async def test_by_event_generate_is_not_captured_by_meeting_id_route(api):
    # Proves POST /by-event/generate precedes POST /{meeting_id}/generate:
    # if captured, meeting_id="by-event" with empty context would still 201 but
    # WITHOUT a calendar_event_uid. Assert the link is present.
    with TestClient(api["app"]) as c:
        r = c.post("/api/prep/by-event/generate", headers=_headers(), json=_body("EK2:2000"))
        assert r.status_code == 201
        assert r.json()["calendar_event_uid"] == "EK2:2000"


@pytest.mark.asyncio
async def test_by_event_regenerate_returns_newest(api):
    with TestClient(api["app"]) as c:
        c.post("/api/prep/by-event/generate", headers=_headers(), json=_body("EK3:3000"))
        b = _body("EK3:3000")  # regenerate with a changed title
        b["title"] = "Renamed"
        c.post("/api/prep/by-event/generate", headers=_headers(), json=b)
        got = c.get("/api/prep/by-event/EK3:3000", headers=_headers())
        assert got.status_code == 200
        # newest wins (both rows share the uid; get_by_calendar_event orders by generated_at DESC)
        assert got.json()["content_markdown"]  # a briefing is returned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_prep.py -v`
Expected: FAIL — `/api/prep/by-event/...` routes don't exist (404 / captured by `/{meeting_id}`).

- [ ] **Step 3: Write minimal implementation**

In `src/api/routes/prep.py`: add imports at the top (below the existing imports):

```python
from pydantic import BaseModel, Field

from src.prep.sweep import event_signature
```

Then insert these **between** `@router.get("/prepared-events")` (ends line ~41) and `@router.get("/{meeting_id}")` (line ~44):

```python
class _Attendee(BaseModel):
    name: str = ""
    email: str = ""


class _GenerateEventBody(BaseModel):
    event_uid: str = Field(min_length=1)
    title: str = ""
    attendees: list[_Attendee] = Field(default_factory=list)
    attendee_names: list[str] = Field(default_factory=list)
    end_ts: float
    series_id: str | None = None


@router.get("/by-event/{event_uid}")
async def get_briefing_by_event(event_uid: str, response: Response):
    briefing = await _get_repo().get_by_calendar_event(event_uid)
    if not briefing:
        response.status_code = 204
        return None
    return briefing


@router.post("/by-event/generate", status_code=201)
async def generate_briefing_by_event(body: _GenerateEventBody):
    if not _generator:
        raise HTTPException(status_code=503, detail="Briefing generator not available")
    emails = [a.email for a in body.attendees if a.email]
    sig = event_signature(emails)
    await _generator.generate(
        title=body.title,
        attendees=emails,
        attendee_names=body.attendee_names,
        series_id=body.series_id,
        calendar_event_uid=body.event_uid,
        event_signature=sig,
        expires_at=body.end_ts,
    )
    return await _get_repo().get_by_calendar_event(body.event_uid)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_prep.py -v`
Expected: PASS (all four tests).
Run: `ruff check src/api/routes/prep.py tests/test_api_prep.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/prep.py tests/test_api_prep.py
git commit -m "feat(api): prep by-event get + generate endpoints"
```

---

### Task 2: UI API client — prep by-event

**Files:**

- Modify: `ui/src/lib/api.ts`, `ui/src/lib/types.ts`
- Test: `ui/src/lib/__tests__/api.test.ts`

**Interfaces:**

- Consumes: `PrepBriefing` type (exists); `request`/`requestRaw` helpers.
- Produces: `interface PrepGenerateEventBody`; `getPrepByEvent(uid): Promise<PrepBriefing | null>`; `generatePrepForEvent(body): Promise<PrepBriefing>`.

- [ ] **Step 1: Write the failing test**

Add to `ui/src/lib/__tests__/api.test.ts` (add `getPrepByEvent`, `generatePrepForEvent` to the import from `../api`):

```typescript
describe("prep by-event", () => {
  it("getPrepByEvent returns null on 204", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 204 }),
    ) as unknown as typeof fetch;
    const res = await getPrepByEvent("EK1:1000");
    expect(res).toBeNull();
  });

  it("generatePrepForEvent POSTs the event body", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(
          JSON.stringify({ id: "p1", content_markdown: "x" }),
          {
            status: 201,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
    await generatePrepForEvent({
      event_uid: "EK1:1000",
      title: "Sync",
      attendees: [{ name: "A", email: "a@x.com" }],
      attendee_names: ["A"],
      end_ts: 123,
      series_id: null,
    });
    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/prep/by-event/generate");
    expect(JSON.parse(call?.init?.body as string).event_uid).toBe("EK1:1000");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- api.test`
Expected: FAIL — functions not exported.

- [ ] **Step 3: Write minimal implementation**

In `ui/src/lib/types.ts` add:

```typescript
export interface PrepGenerateEventBody {
  event_uid: string;
  title: string;
  attendees: { name: string; email: string }[];
  attendee_names: string[];
  end_ts: number;
  series_id?: string | null;
}
```

In `ui/src/lib/api.ts` add `PrepGenerateEventBody` to the `import type { ... } from "./types"` block, then in the `// --- Prep Briefings ---` section add:

```typescript
export async function getPrepByEvent(
  eventUid: string,
): Promise<PrepBriefing | null> {
  const res = await requestRaw(
    `/api/prep/by-event/${encodeURIComponent(eventUid)}`,
  );
  if (res.status === 204) return null;
  return res.json() as Promise<PrepBriefing>;
}

export async function generatePrepForEvent(
  body: PrepGenerateEventBody,
): Promise<PrepBriefing> {
  return request<PrepBriefing>("/api/prep/by-event/generate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- api.test`
Expected: PASS.
Run: `cd ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/types.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): prep by-event API client"
```

---

### Task 3: `PrepModal` component

**Files:**

- Create: `ui/src/components/calendar/PrepModal.tsx`
- Test: `ui/src/components/calendar/__tests__/PrepModal.test.tsx`

**Interfaces:**

- Consumes: `getPrepByEvent(uid)` (Task 2).
- Produces: `function PrepModal({ eventUid, title, onClose }: { eventUid: string; title: string; onClose: () => void })`.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/PrepModal.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { PrepModal } from "../PrepModal";

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("PrepModal", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ id: "p1", content_markdown: "## Prep\nAlice notes", expires_at: 9e9 }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    ) as unknown as typeof fetch;
  });

  it("renders the briefing markdown", async () => {
    render(<PrepModal eventUid="EK1:1000" title="Sync" onClose={() => {}} />, {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(screen.getByText("Prep")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- PrepModal`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Create `ui/src/components/calendar/PrepModal.tsx`:

```typescript
import { useQuery } from "@tanstack/react-query";
import Markdown from "react-markdown";
import { getPrepByEvent } from "../../lib/api";

interface PrepModalProps {
  eventUid: string;
  title: string;
  onClose: () => void;
}

export function PrepModal({ eventUid, title, onClose }: PrepModalProps) {
  const { data: briefing, isLoading } = useQuery({
    queryKey: ["prep", "by-event", eventUid],
    queryFn: () => getPrepByEvent(eventUid),
  });

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-2xl max-h-[80vh] overflow-y-auto rounded-xl border border-border bg-surface-raised p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-text-primary">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-text-muted hover:text-text-primary"
          >
            ✕
          </button>
        </div>
        {isLoading ? (
          <div className="space-y-2">
            <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
            <div className="h-4 w-2/3 bg-surface border border-border rounded animate-pulse" />
          </div>
        ) : briefing ? (
          <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
            <Markdown>{briefing.content_markdown}</Markdown>
          </div>
        ) : (
          <p className="text-sm text-text-muted">No briefing available.</p>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- PrepModal`
Expected: PASS.
Run: `cd ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/PrepModal.tsx ui/src/components/calendar/__tests__/PrepModal.test.tsx
git commit -m "feat(ui): PrepModal for viewing an event's briefing"
```

---

### Task 4: `UpcomingEventCard` interactive actions

**Files:**

- Modify: `ui/src/components/calendar/UpcomingEventCard.tsx`
- Test: `ui/src/components/calendar/__tests__/EventActions.test.tsx`

**Interfaces:**

- Consumes: `generatePrepForEvent(body)`, `startRecording()` (api); `PrepModal` (Task 3); `useDaemonStatus()` (`{ state }`).
- Produces: interactive popover — View/Generate/Regenerate prep + gated Record.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/EventActions.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";

vi.mock("../../../hooks/useDaemonStatus", () => ({
  useDaemonStatus: () => ({ state: "idle", daemonRunning: true, activeMeeting: null }),
}));

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function liveEvent(): CalendarEvent {
  const now = Math.floor(Date.now() / 1000);
  return {
    event_uid: "EK1:1000", title: "Standup", start_ts: now - 60, end_ts: now + 600,
    attendees: [{ name: "Alice", email: "a@x.com" }], organizer: null, join_url: "",
    meeting_id: "", calendar_name: "Work",
  };
}

describe("UpcomingEventCard actions", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/prep/by-event/generate")) {
        return new Response(JSON.stringify({ id: "p1", content_markdown: "x" }), {
          status: 201, headers: { "content-type": "application/json" },
        });
      }
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;
  });

  it("shows Generate when not prepared and posts on click", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ id: "p1", content_markdown: "x" }), {
        status: 201, headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    render(<UpcomingEventCard event={liveEvent()} />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Standup/i }));
    fireEvent.click(screen.getByText(/Generate prep/i));
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/prep/by-event/generate"))).toBe(true),
    );
  });

  it("Record button confirms then starts recording when live", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ status: "recording", started_at: 1 }), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    render(<UpcomingEventCard event={liveEvent()} />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Standup/i }));
    fireEvent.click(screen.getByText(/Record this meeting/i));
    fireEvent.click(screen.getByText(/Start recording\?/i)); // two-step confirm
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/record/start"))).toBe(true),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- EventActions`
Expected: FAIL — the popover has no Generate/Record actions.

- [ ] **Step 3: Write minimal implementation**

Replace `ui/src/components/calendar/UpcomingEventCard.tsx` with:

```typescript
import { useState } from "react";
import { format } from "date-fns";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { CalendarEvent } from "../../lib/types";
import { generatePrepForEvent, startRecording } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { PrepModal } from "./PrepModal";

interface UpcomingEventCardProps {
  event: CalendarEvent;
  compact?: boolean;
  preparedUids?: Set<string>;
}

/** Renders an imported (not-yet-recorded) calendar event, with interactive prep/record actions. */
export function UpcomingEventCard({
  event,
  compact = false,
  preparedUids,
}: UpcomingEventCardProps) {
  const [open, setOpen] = useState(false);
  const [showPrep, setShowPrep] = useState(false);
  const [confirmingRecord, setConfirmingRecord] = useState(false);
  const queryClient = useQueryClient();
  const { state } = useDaemonStatus();

  const title = event.title || "Untitled";
  const start = format(new Date(event.start_ts * 1000), "HH:mm");
  const prepared = preparedUids?.has(event.event_uid) ?? false;

  const isRecording = state === "recording";
  const nowSec = Date.now() / 1000;
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
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["prepared-events"] });
      void queryClient.invalidateQueries({ queryKey: ["prep", "by-event", event.event_uid] });
      setShowPrep(true);
    },
  });

  const record = useMutation({
    mutationFn: () => startRecording(),
    onSuccess: () => setConfirmingRecord(false),
  });

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={title}
        className={`w-full text-left rounded border border-dashed border-text-muted/40 bg-surface-hover/40 text-text-secondary hover:border-accent/50 transition-colors ${
          compact ? "px-1 py-0.5 text-[10px]" : "px-2 py-1 text-xs"
        }`}
      >
        <span className="truncate block">
          {!compact && <span className="text-text-muted mr-1">{start}</span>}
          {title}
          {prepared && (
            <span className="ml-1 rounded bg-accent/20 text-accent px-1 text-[9px] align-middle">
              Prep ready
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="absolute z-10 mt-1 w-56 rounded-lg border border-border bg-surface-raised p-3 shadow-lg text-xs">
          <p className="font-medium text-text-primary">{title}</p>
          <p className="text-text-muted mt-0.5">
            {format(new Date(event.start_ts * 1000), "EEE d MMM, HH:mm")} –{" "}
            {format(new Date(event.end_ts * 1000), "HH:mm")}
          </p>
          {event.attendees.length > 0 && (
            <ul className="mt-2 flex flex-col gap-0.5">
              {event.attendees.map((a) => (
                <li key={a.email || a.name} className="text-text-secondary">
                  {a.name || a.email}
                </li>
              ))}
            </ul>
          )}
          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-accent hover:underline"
            >
              Join
            </a>
          )}

          <div className="mt-3 flex flex-col gap-1.5 border-t border-border pt-2">
            {prepared && (
              <button
                type="button"
                onClick={() => setShowPrep(true)}
                className="text-left text-accent hover:underline"
              >
                View prep
              </button>
            )}
            <button
              type="button"
              onClick={() => generate.mutate()}
              disabled={generate.isPending}
              className="text-left text-accent hover:underline disabled:opacity-50"
            >
              {generate.isPending
                ? "Generating..."
                : prepared
                  ? "Regenerate prep"
                  : "Generate prep"}
            </button>

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
                className="text-left text-accent hover:underline disabled:opacity-40 disabled:no-underline disabled:text-text-muted"
              >
                Record this meeting
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => record.mutate()}
                  disabled={record.isPending}
                  className="text-left text-accent hover:underline disabled:opacity-50"
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
              </div>
            )}
          </div>
        </div>
      )}

      {showPrep && (
        <PrepModal
          eventUid={event.event_uid}
          title={title}
          onClose={() => setShowPrep(false)}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- EventActions`
Expected: PASS (both tests).
Run: `cd ui && npm test` — Expected: existing calendar tests (incl. PrepBadge) still PASS (the `preparedUids` prop is unchanged; the badge test doesn't exercise the new buttons but still renders).
Run: `cd ui && npx tsc --noEmit` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/UpcomingEventCard.tsx ui/src/components/calendar/__tests__/EventActions.test.tsx
git commit -m "feat(ui): interactive prep/record actions in event popover"
```

---

### Task 5: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Python subset + lint**

Run: `python3 -m pytest tests/test_api_prep.py -v`
Expected: PASS.
Run: `ruff check src/ tests/`
Expected: clean.
(If the environment can run it, `python3 -m pytest tests/ -q` for the full suite; otherwise note it was not run.)

- [ ] **Step 2: UI suite + types**

Run: `cd ui && npm test`
Expected: PASS (all, incl. the new PrepModal / EventActions / api tests).
Run: `cd ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit (only if lint/format fixups were needed)**

```bash
git add -A
git commit -m "chore(popover-actions): lint + test fixups"
```

(Skip if nothing changed.)

---

## Self-Review

**Spec coverage:**

- `GET /api/prep/by-event/{uid}` (204 when none) → Task 1. ✔
- `POST /api/prep/by-event/generate` (body-carried, before `/{meeting_id}/generate`, bypasses filter, `expires_at=end_ts`, server-computed signature) → Task 1. ✔
- Reuse `/api/record/start` untouched → Task 4 (`startRecording()`). ✔
- `PrepModal` (markdown over calendar) → Task 3. ✔
- Popover View/Generate/Regenerate + gated Record (live && !isRecording, two-step confirm) → Task 4. ✔
- UI api client → Task 2. ✔
- No migration/config → confirmed (none added). ✔

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `generatePrepForEvent(PrepGenerateEventBody)` and `getPrepByEvent(uid)` consistent across Tasks 2, 3, 4. `PrepGenerateEventBody` fields match the backend `_GenerateEventBody` (Task 1): `event_uid/title/attendees[{name,email}]/attendee_names/end_ts/series_id`. `useDaemonStatus().state === "recording"` matches the confirmed hook shape. `PrepModal({ eventUid, title, onClose })` consistent Tasks 3, 4.

**Route-ordering note (Task 1):** the new routes are inserted **between** `/prepared-events` and `/{meeting_id}`, so `POST /by-event/generate` precedes `POST /{meeting_id}/generate` — the `test_by_event_generate_is_not_captured_by_meeting_id_route` test guards this.
