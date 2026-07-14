# Meeting Bug-Fix Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five user-reported bugs — un-editable tags/labels, no explicit "apply label", "thank you" silence hallucinations, speaker→person assignment failing, and system audio not being captured — each as an isolated, test-driven change.

**Architecture:** Python daemon (`src/`, FastAPI + SQLite via aiosqlite) + Tauri/React UI (`ui/`). Backend changes follow the existing numbered-migration + repository + route pattern; UI changes follow the existing react-query + component pattern. Bugs are independent and land in the order below.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio (auto mode), aiosqlite, FastAPI, Pydantic; React 19 + TypeScript + Vite, TanStack Query, Vitest 4 + @testing-library/react.

## Global Constraints

- **British spelling** in all new comments/log messages/identifiers (summarise, diariser, normalise) — match the codebase.
- **pytest-asyncio is in `asyncio_mode = "auto"`** — do NOT add `@pytest.mark.asyncio` to any test or fixture.
- **DB tests** use the conftest `db` / `repo` async fixtures (a `tmp_path` SQLite DB; migrations run inside `Database.connect()`). Never hand-run migration SQL.
- **Two autouse conftest fixtures** run on every test: mic is forced `authorized`, `CoreAudioBackend.available` is forced `False`. To test routing, inject a `FakeBackend`; never call `src.mic_permission.request_access` in a test.
- **FastAPI route order:** any new `GET /api/meetings/<literal>` MUST be declared before `GET /api/meetings/{meeting_id}` or the literal is captured as an id.
- **Keep the `meetings.label` column and dataclass field** — do not `DROP COLUMN` (SQLite makes it painful; retiring it is a later migration). It just stops being surfaced.
- **Config additions are backward-compatible** (`_build_dataclass()` ignores unknown keys); keep new defaults conservative.
- **Lint:** `ruff check src/ tests/` (line-length 100, rules E/F/W/I). **Typecheck UI:** `cd ui && npx tsc --noEmit`.
- **Commits:** one deliverable per commit, conventional-commit subject; every commit message ends with the trailer `Claude-Session: https://claude.ai/code/session_01DZ4xa1rkJNP7a495pnVDc1` (appended to each `git commit` below, omitted from the snippets for brevity).
- **Run commands from repo root** (`.../fix-unawaited-coroutine-warning`); UI commands from `ui/`.

---

## Bug #1 + #2 — Unify tags & label into one editable multi-tag control

### Task 1: DB migration v15 — fold `label` into `tags`

**Files:**

- Modify: `src/db/database.py` (`SCHEMA_VERSION` at line 25; the migration ladder ~662-671)
- Test: `tests/test_db_migration_v15.py` (create)

**Interfaces:**

- Produces: `SCHEMA_VERSION == 15`; after connecting a v14 DB, every meeting's `tags` JSON array contains any previously non-empty `label` value.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_v15.py`. It seeds a meeting at head, rewinds `user_version` to 14, reconnects to trigger the `< 15` fold, and asserts the label now lives in `tags`. (This is schema-agnostic: v15 adds no tables/columns, so a head DB rewound to 14 is a faithful pre-fold state.)

```python
import json

from src.db.database import Database, SCHEMA_VERSION
from src.db.repository import MeetingRepository


async def test_migration_folds_label_into_tags(tmp_path):
    db_path = tmp_path / "v14_fold.db"

    db = Database(db_path=db_path)
    await db.connect()
    repo = MeetingRepository(db)
    meeting_id = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(meeting_id, tags=["standup"], label="ClientX")
    # Rewind so the < 15 migration re-runs on the next connect().
    await db.conn.execute("PRAGMA user_version = 14")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db2.conn.execute(
            "SELECT tags FROM meetings WHERE id = ?", (meeting_id,)
        )
        tags = json.loads((await cur.fetchone())["tags"])
        assert "standup" in tags
        assert "ClientX" in tags
    finally:
        await db2.close()


async def test_migration_leaves_empty_label_meetings_untouched(tmp_path):
    db = Database(db_path=tmp_path / "v14_empty.db")
    await db.connect()
    repo = MeetingRepository(db)
    meeting_id = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(meeting_id, tags=["standup"])
    await db.conn.execute("PRAGMA user_version = 14")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=tmp_path / "v14_empty.db")
    await db2.connect()
    try:
        cur = await db2.conn.execute(
            "SELECT tags FROM meetings WHERE id = ?", (meeting_id,)
        )
        assert json.loads((await cur.fetchone())["tags"]) == ["standup"]
    finally:
        await db2.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v15.py -v`
Expected: FAIL — `assert (await cur.fetchone())[0] == SCHEMA_VERSION` fails because `SCHEMA_VERSION` is still 14 (or the rewound DB stays at 14 with no fold).

- [ ] **Step 3: Bump the version and add the fold migration**

In `src/db/database.py`:

1. Confirm `import json` is present at the top; add it if absent.
2. Change `SCHEMA_VERSION = 14` → `SCHEMA_VERSION = 15`.
3. In the v14 migration block, change its terminal PRAGMA from the interpolated version to a **literal 14** so v14 stays intermediate, then add the new `< 15` block before the final `else`:

```python
        if current_version < 14:
            # Keyword trackers: user-defined topics watched across meetings.
            await self.conn.executescript(TRACKERS_SQL)
            await self.conn.executescript(TRACKER_HITS_SQL)
            await self.conn.execute("PRAGMA user_version = 14")
            await self.conn.commit()
            logger.info("Database migrated to version 14 (keyword trackers)")
            current_version = 14

        if current_version < 15:
            # Fold the single free-text `label` into the `tags` array so the
            # UI can present one editable multi-tag control. Pure data move —
            # the `label` column is retained for back-compat.
            cursor = await self.conn.execute(
                "SELECT id, tags, label FROM meetings WHERE label != ''"
            )
            for row in await cursor.fetchall():
                existing = json.loads(row["tags"]) if row["tags"] else []
                if row["label"] not in existing:
                    existing.append(row["label"])
                    await self.conn.execute(
                        "UPDATE meetings SET tags = ? WHERE id = ?",
                        (json.dumps(existing), row["id"]),
                    )
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 15 (fold label into tags)")
            current_version = 15
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_db_migration_v15.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Run the migration + repository suites to check for regressions**

Run: `python3 -m pytest tests/test_db_migration_v14.py tests/test_repository.py -q`
Expected: PASS (v14 test still green now that its PRAGMA is a literal 14).

- [ ] **Step 6: Commit**

```bash
git add src/db/database.py tests/test_db_migration_v15.py
git commit -m "feat(db): migration v15 folds meeting label into tags"
```

### Task 2: Repository `get_distinct_tags`

**Files:**

- Modify: `src/db/repository.py` (near `get_distinct_labels`, ~429)
- Test: `tests/test_repository.py`

**Interfaces:**

- Produces: `async def get_distinct_tags(self) -> list[str]` — sorted unique non-empty tags across all meetings.

- [ ] **Step 1: Write the failing test** (add to `tests/test_repository.py`)

```python
async def test_get_distinct_tags_returns_sorted_unique(repo):
    m1 = await repo.create_meeting(started_at=1000.0)
    m2 = await repo.create_meeting(started_at=2000.0)
    await repo.update_meeting(m1, tags=["budget", "acme"])
    await repo.update_meeting(m2, tags=["acme", "planning"])
    assert await repo.get_distinct_tags() == ["acme", "budget", "planning"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_repository.py::test_get_distinct_tags_returns_sorted_unique -v`
Expected: FAIL — `AttributeError: 'MeetingRepository' object has no attribute 'get_distinct_tags'`.

- [ ] **Step 3: Implement `get_distinct_tags`** (add directly after `get_distinct_labels`)

```python
    async def get_distinct_tags(self) -> list[str]:
        """Return all unique non-empty tags across meetings, sorted."""
        cursor = await self._db.conn.execute(
            "SELECT DISTINCT je.value FROM meetings, json_each(meetings.tags) je "
            "WHERE meetings.tags IS NOT NULL AND je.value != '' "
            "ORDER BY je.value"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_repository.py::test_get_distinct_tags_returns_sorted_unique -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/db/repository.py tests/test_repository.py
git commit -m "feat(db): add get_distinct_tags repository query"
```

### Task 3: API routes — `GET /api/meetings/tags` + `PATCH /api/meetings/{id}/tags`

**Files:**

- Modify: `src/api/routes/meetings.py` (`SetLabelRequest` ~35; `/labels` ~135; `/label` ~217)
- Test: `tests/test_api_meetings_extra.py`

**Interfaces:**

- Consumes: `MeetingRepository.get_distinct_tags`, `MeetingRepository.update_meeting(meeting_id, tags=...)`.
- Produces: `GET /api/meetings/tags` → `{"tags": [...]}`; `PATCH /api/meetings/{id}/tags` body `{"tags": [...]}` → `{"meeting_id": id, "tags": [normalised]}`. Tags are trimmed, empties dropped, de-duped preserving order.

- [ ] **Step 1: Write the failing tests** (add to `tests/test_api_meetings_extra.py`, using the existing `client` fixture that yields `(client, repo)`)

```python
async def test_get_meeting_tags_returns_distinct(client):
    c, repo = client
    m1 = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(m1, tags=["acme", "budget"])
    resp = c.get("/api/meetings/tags", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["acme", "budget"]


async def test_patch_meeting_tags_persists_and_normalises(client):
    c, repo = client
    m1 = await repo.create_meeting(started_at=1000.0)
    resp = c.patch(
        f"/api/meetings/{m1}/tags",
        headers=_auth_headers(),
        json={"tags": ["  budget ", "budget", "", "planning"]},
    )
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["budget", "planning"]
    meeting = await repo.get_meeting(m1)
    assert meeting.tags == ["budget", "planning"]


async def test_patch_meeting_tags_missing_meeting_404s(client):
    c, _ = client
    resp = c.patch(
        "/api/meetings/nope/tags", headers=_auth_headers(), json={"tags": ["x"]}
    )
    assert resp.status_code == 404
```

> If `test_api_meetings_extra.py` defines its auth header helper under a different name, reuse whatever the existing tests in that file call (grep the file for `headers=` to confirm — Task uses `_auth_headers()` as seen in `tests/test_api_people.py`).

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_api_meetings_extra.py -k tags -v`
Expected: FAIL — 404/405 (routes not defined) on the GET/PATCH.

- [ ] **Step 3: Implement the request model and routes**

Add next to `SetLabelRequest`:

```python
class SetTagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=50)
```

Add the GET route immediately after `get_meeting_labels` (still above the `/{meeting_id}` catch-all):

```python
@router.get("/api/meetings/tags", summary="Get distinct meeting tags")
async def get_meeting_tags():
    return {"tags": await _repo.get_distinct_tags()}
```

Add the PATCH route next to `set_meeting_label`:

```python
@router.patch("/api/meetings/{meeting_id}/tags", summary="Set meeting tags")
async def set_meeting_tags(meeting_id: str, body: SetTagsRequest):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    normalised: list[str] = []
    for raw in body.tags:
        tag = raw.strip()
        if tag and tag not in normalised:
            normalised.append(tag)
    await _repo.update_meeting(meeting_id, tags=normalised)
    return {"meeting_id": meeting_id, "tags": normalised}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_api_meetings_extra.py -k tags -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/ tests/`

```bash
git add src/api/routes/meetings.py tests/test_api_meetings_extra.py
git commit -m "feat(api): add GET/PATCH meeting tags endpoints"
```

### Task 4: UI API client — `setMeetingTags` / `getMeetingTags`

**Files:**

- Modify: `ui/src/lib/api.ts` (`setMeetingLabel`/`getMeetingLabels` ~382-395)
- Test: `ui/src/lib/__tests__/api.test.ts`

**Interfaces:**

- Produces: `setMeetingTags(id: string, tags: string[]): Promise<void>`, `getMeetingTags(): Promise<string[]>`.

- [ ] **Step 1: Write the failing test** (add to `ui/src/lib/__tests__/api.test.ts`, mirroring the existing fetch-stub tests there)

```ts
it("setMeetingTags PATCHes the tags array", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: input.toString(), init });
      return new Response("{}", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  ) as unknown as typeof fetch;

  await setMeetingTags("m1", ["a", "b"]);

  const call = calls.find((c) => c.init?.method === "PATCH");
  expect(call?.url).toContain("/api/meetings/m1/tags");
  expect(JSON.parse(call?.init?.body as string)).toEqual({ tags: ["a", "b"] });
});

it("getMeetingTags returns the tags array", async () => {
  globalThis.fetch = vi.fn(
    async () =>
      new Response(JSON.stringify({ tags: ["x", "y"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  ) as unknown as typeof fetch;
  expect(await getMeetingTags()).toEqual(["x", "y"]);
});
```

Add `setMeetingTags, getMeetingTags` to the import from `../api` at the top of the test file.

- [ ] **Step 2: Run to verify it fails**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`
Expected: FAIL — `setMeetingTags is not a function` / import error.

- [ ] **Step 3: Implement the two functions** (add after `getMeetingLabels` in `ui/src/lib/api.ts`)

```ts
export async function setMeetingTags(
  id: string,
  tags: string[],
): Promise<void> {
  await request(`/api/meetings/${encodeURIComponent(id)}/tags`, {
    method: "PATCH",
    body: JSON.stringify({ tags }),
  });
}

export async function getMeetingTags(): Promise<string[]> {
  const data = await request<{ tags: string[] }>("/api/meetings/tags");
  return data.tags;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): add setMeetingTags/getMeetingTags API client fns"
```

### Task 5: UI — `TagEditor` component, wire into MeetingDetail, drop LabelEditor

**Files:**

- Create: `ui/src/components/meetings/TagEditor.tsx`
- Create: `ui/src/components/meetings/__tests__/TagEditor.test.tsx`
- Modify: `ui/src/components/meetings/MeetingDetail.tsx` (imports ~6-17; `LabelEditor` 314-422; header render 666-683)
- Modify: `ui/src/components/meetings/MeetingList.tsx` (redundant `m.label` pill ~430-434)

**Interfaces:**

- Consumes: `getMeetingTags`, `setMeetingTags` (Task 4); `useToast` (`ToastProvider` context).
- Produces: `<TagEditor meetingId={string} tags={string[]} />` — removable chips + Add-on-Enter/button; no blur auto-commit.

- [ ] **Step 1: Write the failing component test**

Create `ui/src/components/meetings/__tests__/TagEditor.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { TagEditor } from "../TagEditor";
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

describe("TagEditor", () => {
  let calls: { url: string; method: string; body: unknown }[];

  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        const method = init?.method ?? "GET";
        const body = init?.body ? JSON.parse(init.body as string) : undefined;
        calls.push({ url, method, body });
        if (url.includes("/api/meetings/tags")) {
          return new Response(JSON.stringify({ tags: ["existing"] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;
  });

  it("adds a tag via Enter and PATCHes the full array", async () => {
    render(<TagEditor meetingId="m1" tags={["budget"]} />, {
      wrapper: makeWrapper(),
    });
    const input = screen.getByLabelText("Add meeting tag");
    fireEvent.change(input, { target: { value: "planning" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.url).toContain("/api/meetings/m1/tags");
      expect(patch?.body).toEqual({ tags: ["budget", "planning"] });
    });
  });

  it("removes a tag and PATCHes the remaining array", async () => {
    render(<TagEditor meetingId="m1" tags={["budget", "planning"]} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByLabelText("Remove tag budget"));
    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH");
      expect(patch?.body).toEqual({ tags: ["planning"] });
    });
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/TagEditor.test.tsx`
Expected: FAIL — cannot resolve `../TagEditor`.

- [ ] **Step 3: Create the `TagEditor` component**

Create `ui/src/components/meetings/TagEditor.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getMeetingTags, setMeetingTags } from "../../lib/api";
import { useToast } from "../common/Toast";

/**
 * Editable multi-tag chip control for a meeting. Every tag (including
 * auto-generated summary tags and folded labels) is a removable chip; new
 * tags are added explicitly via Enter or the Add button — never on blur.
 */
export function TagEditor({
  meetingId,
  tags,
}: {
  meetingId: string;
  tags: string[];
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const { data: allTags = [] } = useQuery({
    queryKey: ["meeting-tags"],
    queryFn: getMeetingTags,
    staleTime: 30_000,
  });

  const saveTags = useMutation({
    mutationFn: (next: string[]) => setMeetingTags(meetingId, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      queryClient.invalidateQueries({ queryKey: ["meeting-tags"] });
    },
    onError: () => {
      toast.error("Failed to save tags.");
    },
  });

  const addTag = (raw: string) => {
    const tag = raw.trim();
    setDraft("");
    setOpen(false);
    if (!tag || tags.includes(tag)) return;
    saveTags.mutate([...tags, tag]);
  };

  const removeTag = (tag: string) => {
    saveTags.mutate(tags.filter((t) => t !== tag));
  };

  const suggestions = allTags.filter(
    (t) => !tags.includes(t) && t.toLowerCase().includes(draft.toLowerCase()),
  );

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div
      className="relative inline-flex flex-wrap items-center gap-1.5"
      ref={wrapperRef}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-accent/10 text-accent"
        >
          {tag}
          <button
            onClick={() => removeTag(tag)}
            aria-label={`Remove tag ${tag}`}
            className="opacity-60 hover:opacity-100 transition-opacity"
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            addTag(draft);
          }
          if (e.key === "Escape") {
            setDraft("");
            setOpen(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
        placeholder="Add tag..."
        aria-label="Add meeting tag"
        className="px-2 py-1 text-xs rounded-md bg-surface-raised border border-border text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent w-28"
      />
      <button
        onClick={() => addTag(draft)}
        disabled={!draft.trim() || saveTags.isPending}
        className="text-xs px-2 py-1 rounded-md bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-40 transition-colors"
      >
        Add
      </button>
      {open && suggestions.length > 0 && (
        <div className="absolute left-0 top-full mt-1 w-40 rounded-lg bg-surface-raised border border-border shadow-lg z-10 py-1 max-h-32 overflow-y-auto">
          {suggestions.map((tag) => (
            <button
              key={tag}
              onMouseDown={(e) => {
                e.preventDefault();
                addTag(tag);
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              {tag}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run the component test to verify it passes**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/TagEditor.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire `TagEditor` into `MeetingDetail` and delete `LabelEditor`**

In `ui/src/components/meetings/MeetingDetail.tsx`:

1. In the import block (~6-17), remove `setMeetingLabel` and `getMeetingLabels`, and add `import { TagEditor } from "./TagEditor";`.
2. Delete the entire `LabelEditor` function (lines 314-422).
3. Replace the header's tags-pills block **and** the `LabelEditor` render (lines 666-683) with:

```tsx
{
  /* Tags (editable) */
}
<div className="mt-2">
  <TagEditor meetingId={meeting.id} tags={meeting.tags} />
</div>;
```

- [ ] **Step 6: Remove the now-redundant label pill in `MeetingList`**

In `ui/src/components/meetings/MeetingList.tsx`, delete the `{m.label && (...)}` purple pill block (~430-434). Folded label values now appear as normal tag chips.

- [ ] **Step 7: Confirm `setMeetingLabel`/`getMeetingLabels` have no remaining callers, then remove them**

Run: `cd ui && grep -rn "setMeetingLabel\|getMeetingLabels" src`

- If the only matches are their definitions in `src/lib/api.ts`, delete both functions from `api.ts`.
- If any other file still imports them, leave them in place (note it) and move on.

- [ ] **Step 8: Typecheck + full UI test run**

Run: `cd ui && npx tsc --noEmit && npm test`
Expected: PASS, no type errors (in particular no "unused import" from the removed label fns).

- [ ] **Step 9: Commit**

```bash
git add ui/src/components/meetings/TagEditor.tsx \
  ui/src/components/meetings/__tests__/TagEditor.test.tsx \
  ui/src/components/meetings/MeetingDetail.tsx \
  ui/src/components/meetings/MeetingList.tsx \
  ui/src/lib/api.ts
git commit -m "feat(ui): editable multi-tag TagEditor replaces read-only tags + label"
```

---

## Bug #4 — Speaker→person assignment fails on real diariser labels

### Task 6: Widen `_SPEAKER_ID_RE` so labels like "Me + Remote" are accepted

**Files:**

- Modify: `src/api/routes/people.py` (`_SPEAKER_ID_RE`, line 32)
- Test: `tests/test_api_people.py`

**Interfaces:**

- Produces: `POST /api/meetings/{id}/speakers/{speaker_id}/assign-person` accepts any non-empty, control-char-free label (≤200 chars), including `+`, `:`, apostrophes, accents.

- [ ] **Step 1: Write the failing regression test** (add to `tests/test_api_people.py`, mirroring `test_assign_person_renames_speaker_and_links_person`)

```python
async def test_assign_person_accepts_overlap_label(api):
    repo = api["repo"]
    person_repo = api["person_repo"]

    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    transcript = {
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "hello", "speaker": "Me + Remote"},
            {"start": 3.0, "end": 6.0, "text": "hi", "speaker": "Me"},
        ]
    }
    await repo.update_meeting(meeting_id, transcript_json=json.dumps(transcript))
    person_id = await person_repo.create(name="Sarah Chen")

    with TestClient(api["app"]) as c:
        resp = c.post(
            f"/api/meetings/{meeting_id}/speakers/Me + Remote/assign-person",
            headers=_auth_headers(),
            json={"person_id": person_id, "enrol_voice": False},
        )

    assert resp.status_code == 200
    meeting = await repo.get_meeting(meeting_id)
    segments = json.loads(meeting.transcript_json)["segments"]
    assert segments[0]["speaker"] == "Sarah Chen"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_api_people.py::test_assign_person_accepts_overlap_label -v`
Expected: FAIL — `assert resp.status_code == 200` gets 422 ("Invalid speaker_id format"), because `+` is rejected.

- [ ] **Step 3: Widen the regex** (`src/api/routes/people.py:32`)

Replace:

```python
_SPEAKER_ID_RE = re.compile(r"^[a-zA-Z0-9_ -]+$")
```

with (any non-empty run of printable characters — no control chars/DEL — up to 200 chars; the value is only ever a parameterised SQL bind and a dict key, so there is no injection surface):

```python
_SPEAKER_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
```

- [ ] **Step 4: Run to verify it passes + no regression on existing people tests**

Run: `python3 -m pytest tests/test_api_people.py -v`
Expected: PASS (new test + all existing).

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/people.py tests/test_api_people.py
git commit -m "fix(api): accept real diariser speaker labels in assign-person"
```

### Task 7: Surface the real backend error in `AssignSpeakerMenu`

**Files:**

- Modify: `ui/src/lib/api.ts` (add `describeApiError` near `ApiError`, ~114)
- Modify: `ui/src/components/people/AssignSpeakerMenu.tsx` (`onError` at ~59 and ~73)
- Test: `ui/src/lib/__tests__/api.test.ts`

**Interfaces:**

- Produces: `describeApiError(error: unknown, fallback: string): string` — appends `ApiError.detail` to `fallback` when present, else returns `fallback`.

- [ ] **Step 1: Write the failing test** (add to `ui/src/lib/__tests__/api.test.ts`)

```ts
it("describeApiError appends the backend detail", () => {
  expect(
    describeApiError(
      new ApiError(422, "Invalid speaker_id format"),
      "Failed to assign person",
    ),
  ).toBe("Failed to assign person: Invalid speaker_id format");
});

it("describeApiError falls back for non-ApiError", () => {
  expect(describeApiError(new Error("boom"), "Failed to assign person")).toBe(
    "Failed to assign person",
  );
});
```

Add `describeApiError, ApiError` to the imports from `../api` in the test file.

- [ ] **Step 2: Run to verify it fails**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`
Expected: FAIL — `describeApiError is not a function`.

- [ ] **Step 3: Implement `describeApiError`** (add after the `ApiError` class in `ui/src/lib/api.ts`)

```ts
/** Build a user-facing message from a thrown error, surfacing the API detail. */
export function describeApiError(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.detail) {
    return `${fallback}: ${error.detail}`;
  }
  return fallback;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`
Expected: PASS.

- [ ] **Step 5: Wire it into `AssignSpeakerMenu`**

In `ui/src/components/people/AssignSpeakerMenu.tsx`:

1. Add `describeApiError` to the import from `../../lib/api` (line 3).
2. Replace the two error handlers:
   - Line 59: `onError: () => toast.error("Failed to assign person"),` → `onError: (e) => toast.error(describeApiError(e, "Failed to assign person")),`
   - Line 73: `onError: () => toast.error("Failed to create person"),` → `onError: (e) => toast.error(describeApiError(e, "Failed to create person")),`

- [ ] **Step 6: Typecheck + commit**

Run: `cd ui && npx tsc --noEmit`

```bash
git add ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts ui/src/components/people/AssignSpeakerMenu.tsx
git commit -m "fix(ui): surface backend error detail when assigning a speaker"
```

---

## Bug #3 — "Thank you" hallucinations during silence

### Task 8: Hallucination helpers (phrase repetition + known-phrase set)

**Files:**

- Modify: `src/transcriber.py` (near `_is_repetition_hallucination` ~98 and `_text_compression_ratio` ~114)
- Test: `tests/test_transcriber.py`

**Interfaces:**

- Produces: module-level `KNOWN_HALLUCINATION_PHRASES: frozenset[str]`; `Transcriber._is_phrase_repetition(text: str, min_repeats: int = 3) -> bool`; `Transcriber._is_known_hallucination_phrase(text: str) -> bool` (normalises: lowercase, strip surrounding whitespace/`.,!?`).

- [ ] **Step 1: Write the failing unit tests** (add to `tests/test_transcriber.py`)

```python
from src.transcriber import Transcriber, KNOWN_HALLUCINATION_PHRASES


def test_is_phrase_repetition_detects_repeated_short_phrase():
    assert Transcriber._is_phrase_repetition("thank you. thank you. thank you.")
    assert Transcriber._is_phrase_repetition("you you you you")


def test_is_phrase_repetition_ignores_normal_speech():
    assert not Transcriber._is_phrase_repetition(
        "thank you all for coming, let us begin the review"
    )


def test_is_known_hallucination_phrase_matches_after_normalising():
    assert "thank you" in KNOWN_HALLUCINATION_PHRASES
    assert Transcriber._is_known_hallucination_phrase("Thank you.")
    assert not Transcriber._is_known_hallucination_phrase("thank you everyone")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_transcriber.py -k "phrase or known" -v`
Expected: FAIL — `ImportError`/`AttributeError` (symbols not defined).

- [ ] **Step 3: Implement the helpers** (add to `src/transcriber.py`, alongside the existing static filters, and the frozenset at module scope)

```python
KNOWN_HALLUCINATION_PHRASES = frozenset(
    {
        "thank you",
        "thank you very much",
        "thank you for watching",
        "thanks for watching",
        "please subscribe",
        "you",
        "bye",
        "bye bye",
    }
)
```

```python
    @staticmethod
    def _is_phrase_repetition(text: str, min_repeats: int = 3) -> bool:
        """Detect a short phrase repeated back-to-back (e.g. 'thank you.
        thank you. thank you.') that the single-word filter misses."""
        parts = [p.strip().lower() for p in re.split(r"[.!?]+", text) if p.strip()]
        if len(parts) >= min_repeats and len(set(parts)) == 1:
            return True
        # Also catch whitespace-separated n-gram repeats without punctuation.
        words = text.lower().split()
        for n in (1, 2, 3):
            if len(words) < n * min_repeats:
                continue
            grams = [tuple(words[i : i + n]) for i in range(0, len(words) - n + 1, n)]
            run = 1
            for i in range(1, len(grams)):
                run = run + 1 if grams[i] == grams[i - 1] else 1
                if run >= min_repeats:
                    return True
        return False

    @staticmethod
    def _is_known_hallucination_phrase(text: str) -> bool:
        """True if the whole segment is a canonical silence hallucination."""
        normalised = text.strip().lower().strip(".,!?").strip()
        return normalised in KNOWN_HALLUCINATION_PHRASES
```

> `re` is already imported in `src/transcriber.py`; confirm and add `import re` at the top if not.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_transcriber.py -k "phrase or known" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/transcriber.py tests/test_transcriber.py
git commit -m "feat(transcriber): phrase-repetition + known-hallucination detectors"
```

### Task 9: Config knobs + wire the two filters into the batch path

**Files:**

- Modify: `src/utils/config.py` (`TranscriptionConfig`, ~112-132)
- Modify: `config.example.yaml` (transcription block)
- Modify: `src/transcriber.py` (batch loop, ~164-207)
- Test: `tests/test_transcriber.py`

**Interfaces:**

- Consumes: `_is_phrase_repetition`, `_is_known_hallucination_phrase`.
- Produces: `TranscriptionConfig.phrase_repetition_min_repeats: int = 3`, `.hallucination_phrase_no_speech_threshold: float = 0.6`, `.hallucination_phrase_max_words: int = 4`. Batch loop reads `no_speech_prob` per segment and drops matching segments into `dropped_segments`.

- [ ] **Step 1: Write the failing batch tests** (add to `tests/test_transcriber.py`, following the `@patch("src.transcriber.mlx_whisper.transcribe")` convention)

```python
from unittest.mock import patch
from src.transcriber import TranscriptSegment
from src.utils.config import TranscriptionConfig


@patch("src.transcriber.mlx_whisper.transcribe")
def test_thank_you_during_silence_is_dropped(mock_transcribe, tmp_path):
    transcriber = Transcriber(TranscriptionConfig())
    mock_transcribe.return_value = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 3.0, "text": "Real discussion here."},
            {"id": 1, "start": 3.0, "end": 6.0, "text": "Thank you.", "no_speech_prob": 0.95},
        ],
        "language": "en",
    }
    audio_file = tmp_path / "t.wav"
    audio_file.write_bytes(b"\x00" * 100)
    result = transcriber.transcribe(audio_file)
    kept = [s.text for s in result.segments]
    assert "Thank you." not in kept
    assert any("Thank you" in s.text for s in result.dropped_segments)
    assert all(isinstance(s, TranscriptSegment) for s in result.dropped_segments)


@patch("src.transcriber.mlx_whisper.transcribe")
def test_thank_you_in_real_speech_is_kept(mock_transcribe, tmp_path):
    transcriber = Transcriber(TranscriptionConfig())
    mock_transcribe.return_value = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 4.0,
             "text": "Thank you everyone for joining today.", "no_speech_prob": 0.05},
        ],
        "language": "en",
    }
    audio_file = tmp_path / "t.wav"
    audio_file.write_bytes(b"\x00" * 100)
    result = transcriber.transcribe(audio_file)
    assert result.segments[0].text == "Thank you everyone for joining today."


@patch("src.transcriber.mlx_whisper.transcribe")
def test_repeated_thank_you_phrase_is_dropped(mock_transcribe, tmp_path):
    transcriber = Transcriber(TranscriptionConfig())
    mock_transcribe.return_value = {
        "segments": [
            {"id": 0, "start": 0.0, "end": 6.0, "text": "thank you. thank you. thank you."},
        ],
        "language": "en",
    }
    audio_file = tmp_path / "t.wav"
    audio_file.write_bytes(b"\x00" * 100)
    result = transcriber.transcribe(audio_file)
    assert result.segments == []
    assert len(result.dropped_segments) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_transcriber.py -k "thank_you or repeated_thank" -v`
Expected: FAIL — the "Thank you." segment is currently kept.

- [ ] **Step 3: Add the config fields** (`src/utils/config.py`, after `initial_prompt`)

```python
    phrase_repetition_min_repeats: int = 3
    hallucination_phrase_no_speech_threshold: float = 0.6
    hallucination_phrase_max_words: int = 4
```

Add matching commented docs to `config.example.yaml` under the `transcription:` block:

```yaml
# Drop a segment that is a short phrase repeated back-to-back this many
# times (silence hallucination like "thank you. thank you. thank you.").
# phrase_repetition_min_repeats: 3
# Only treat a known filler phrase ("thank you", "thanks for watching") as
# a hallucination when the segment is short and the model reports at least
# this no-speech probability (i.e. emitted during silence).
# hallucination_phrase_no_speech_threshold: 0.6
# hallucination_phrase_max_words: 4
```

- [ ] **Step 4: Wire the filters into the batch loop** (`src/transcriber.py`)

Capture `no_speech` where `start`/`end` are read:

```python
            start = seg_dict["start"]
            end = seg_dict["end"]
            no_speech = seg_dict.get("no_speech_prob", 0.0)
```

Then, immediately after the existing high-compression-ratio filter block (before `segments.append(ts)`), add:

```python
            # Phrase-level repetition (e.g. "thank you. thank you. thank you.").
            if self._is_phrase_repetition(
                text, self._config.phrase_repetition_min_repeats
            ):
                logger.warning(
                    "Skipping phrase-repetition hallucination [%.1f-%.1f]: %s",
                    start, end, text[:80],
                )
                dropped.append(ts)
                continue

            # Known filler phrase emitted during silence.
            if (
                len(text.split()) <= self._config.hallucination_phrase_max_words
                and self._is_known_hallucination_phrase(text)
                and no_speech >= self._config.hallucination_phrase_no_speech_threshold
            ):
                logger.warning(
                    "Skipping silence hallucination [%.1f-%.1f]: %s",
                    start, end, text[:80],
                )
                dropped.append(ts)
                continue
```

- [ ] **Step 5: Run the new + existing transcriber tests**

Run: `python3 -m pytest tests/test_transcriber.py -v`
Expected: PASS — new tests pass; existing tests unaffected (they omit `no_speech_prob`, so it defaults to 0.0 and the silence gate never trips).

- [ ] **Step 6: Lint + commit**

Run: `ruff check src/ tests/`

```bash
git add src/utils/config.py config.example.yaml src/transcriber.py tests/test_transcriber.py
git commit -m "feat(transcriber): drop silence 'thank you' hallucinations in batch path"
```

### Task 10: Apply the same suppression to the live path

**Files:**

- Modify: `src/live_transcriber.py` (imports line 23; emit loop ~270-286)
- Test: `tests/test_live_transcriber.py`

**Interfaces:**

- Consumes: `_is_phrase_repetition`, `_is_known_hallucination_phrase`, config thresholds from `TranscriptionConfig`.
- Produces: live emit loop suppresses (does not emit) hallucinated segments; `_previous_text` is not poisoned by suppressed text.

- [ ] **Step 1: Write the failing live test** (add to `tests/test_live_transcriber.py`, using the existing `_make_lt` + `patch.dict(sys.modules, ...)` pattern)

```python
def test_live_suppresses_silence_thank_you(_make_lt=None):
    from unittest.mock import patch
    import numpy as np

    emitted = []
    fast_result = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Thank you.", "no_speech_prob": 0.95},
        ]
    }
    fake_mlx = type("FakeMLX", (), {"transcribe": staticmethod(lambda *a, **k: fast_result)})
    lt = _make_lt(on_segment=lambda s: emitted.append(s))
    with patch.dict("sys.modules", {"mlx_whisper": fake_mlx}):
        lt._transcribe_chunk(np.zeros(16000, dtype=np.float32))
    assert emitted == []
```

> Match the actual `_make_lt` helper in the file (it is a module-level fixture/helper there, not a parameter — call it as the existing tests do). The assertion is that the hallucinated segment is never emitted.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_live_transcriber.py -k suppresses -v`
Expected: FAIL — the segment is emitted (`emitted` has one item).

- [ ] **Step 3: Import the shared helpers and suppress in the emit loop** (`src/live_transcriber.py`)

Extend the import on line 23 to also bring in the shared detectors:

```python
from src.transcriber import (
    TranscriptSegment,
    Transcriber,
)
```

Inside the `for seg in segments:` emit loop, after computing `seg_text` and the empty-check, add:

```python
            no_speech = seg.get("no_speech_prob", 0.0)
            if Transcriber._is_phrase_repetition(seg_text) or (
                len(seg_text.split()) <= self._config.hallucination_phrase_max_words
                and Transcriber._is_known_hallucination_phrase(seg_text)
                and no_speech >= self._config.hallucination_phrase_no_speech_threshold
            ):
                continue
```

> `self._config` is the `TranscriptionConfig` the LiveTranscriber already holds (confirm the attribute name by grep — it is used for `live_chunk_interval`/thresholds elsewhere in the file; reuse that exact attribute). If the live transcriber does not currently keep the full config, pass the three thresholds in via its constructor to match. Keep the existing chunk-level RMS gate untouched.

- [ ] **Step 4: Run to verify it passes + full live suite**

Run: `python3 -m pytest tests/test_live_transcriber.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/live_transcriber.py tests/test_live_transcriber.py
git commit -m "feat(live): suppress silence hallucinations in live transcription"
```

---

## Bug #5 — System audio not captured (routing did not engage)

### Task 11: Router verifies the default-output switch actually took effect

**Files:**

- Modify: `src/audio_routing.py` (`_ensure_routed_locked`, ~385-404)
- Test: `tests/test_audio_routing.py`

**Interfaces:**

- Produces: `AudioRouter.ensure_routed()` returns `RoutingResult(error=<message>, changed=False)` when, after `set_default_output_device(managed_id)`, the backend's `default_output_device()` does not equal `managed_id`. Previous-output bookkeeping is set only on the confirmed-success path.

- [ ] **Step 1: Write the failing test** (add to `tests/test_audio_routing.py`, mirroring the `Exploding(FakeBackend)` subclass pattern)

```python
def test_routing_that_does_not_stick_returns_error(self):
    class NonSticky(FakeBackend):
        def set_default_output_device(self, device_id):
            # Record the request but leave the default unchanged.
            self.requested = device_id

    backend = NonSticky(_standard_devices(), default_output=1)
    result = _router(backend).ensure_routed()

    assert result.changed is False
    assert result.error is not None
    assert "did not take effect" in result.error.lower()
```

> Place it inside the existing `TestEnsureRouted` (or the routing-failure) class so `_router`, `FakeBackend`, `_standard_devices` are in scope. Confirm `FakeBackend`'s default-output attribute name (it exposes `default_output_device()`); the override simply drops the write.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_audio_routing.py -k does_not_stick -v`
Expected: FAIL — current code returns `changed=True, error=None` even though the switch didn't stick.

- [ ] **Step 3: Add the post-set verification** (`src/audio_routing.py`, in `_ensure_routed_locked` right after `self._backend.set_default_output_device(managed_id)`)

```python
        self._backend.set_default_output_device(managed_id)
        if self._backend.default_output_device() != managed_id:
            current_name = self._backend.device_name(current_id)
            return RoutingResult(
                error=(
                    "System audio routing did not take effect — the default "
                    f"output is still '{current_name}'; system audio will not "
                    "be captured. Route output to a Multi-Output Device "
                    "containing BlackHole in Audio MIDI Setup."
                ),
            )
        self._previous_output_id = current_id
        self._previous_output_uid = current_uid
        self._managed_id = managed_id
```

Ensure the `_previous_output_*` / `_managed_id` assignments that previously followed `set_default_output_device` now live **only** on the success path (below the guard), so `restore()` never reverts a switch that did not happen.

- [ ] **Step 4: Run to verify it passes + full routing suite**

Run: `python3 -m pytest tests/test_audio_routing.py -v`
Expected: PASS — new test passes; existing `test_creates_managed_device_and_switches` still passes (the FakeBackend's `set_default_output_device` really does switch, so verification succeeds).

- [ ] **Step 5: Commit**

```bash
git add src/audio_routing.py tests/test_audio_routing.py
git commit -m "fix(audio): warn when system-audio routing fails to engage"
```

### Task 12: Orchestrator surfaces the routing failure as a pipeline warning

**Files:**

- Test: `tests/test_orchestrator.py` (or `tests/test_audio_routing.py` if orchestrator wiring is covered there)

**Interfaces:**

- Consumes: `AudioRouter.ensure_routed()` error (Task 11); `ContextRecall._ensure_audio_routing()` already emits `pipeline.warning source=routing` on `result.error`.

- [ ] **Step 1: Write the test** — assert `_ensure_audio_routing()` emits `pipeline.warning` with `source="routing"` when the router returns an error.

```python
from unittest.mock import MagicMock
from src.audio_routing import RoutingResult


def test_ensure_audio_routing_emits_warning_on_router_error(app_with_mocked_api):
    app = app_with_mocked_api
    app._config.audio.auto_route_system_audio = True
    app._audio_router = MagicMock()
    app._audio_router.ensure_routed.return_value = RoutingResult(
        error="System audio routing did not take effect."
    )
    emitted = []
    app._emit = MagicMock(side_effect=lambda event, **kw: emitted.append((event, kw)))

    app._ensure_audio_routing()

    warnings = [kw for ev, kw in emitted if ev == "pipeline.warning"]
    assert any(w.get("source") == "routing" for w in warnings)
```

> Confirm `app_with_mocked_api` exposes `_config`, `_audio_router`, `_emit`, and `_ensure_audio_routing` (it constructs a real `ContextRecall`). If `_audio_router` is created lazily, set it before calling. Adjust attribute names to the real ones if the grep in Task 11's map differs.

- [ ] **Step 2: Run to verify it passes** (main.py already has the `if result.error:` branch — this test guards that wiring)

Run: `python3 -m pytest tests/test_orchestrator.py -k ensure_audio_routing -v`
Expected: PASS. If it FAILS because the emit branch is missing, add to `ContextRecall._ensure_audio_routing()`:

```python
        if result.error:
            self._emit("pipeline.warning", source="routing", message=result.error)
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_orchestrator.py src/main.py
git commit -m "test(audio): assert routing failure surfaces a pipeline warning"
```

---

## Task 13: Sprint verification

**Files:** none (verification only)

- [ ] **Step 1: Full Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (~870+ tests, including the new ones).

- [ ] **Step 2: Python lint**

Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 3: UI tests + typecheck**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 4: CodeRabbit review of the branch**

Run: `coderabbit review --agent --base main` (address Critical/Warning findings, re-run until clean or only Info remains).

- [ ] **Step 5: Final status** — summarise per bug: what changed, test counts, and anything explicitly NOT done (e.g. `label` column retained; #5 code guardrail landed but the user's live issue may still be operational routing).

---

## Self-Review

**Spec coverage:**

- Spec #1 (remove/change auto tags) → Tasks 1–5 (migration, repo, routes, client, TagEditor with removable chips). ✔
- Spec #2 (explicit apply, multiple labels) → Task 5 (Add button/Enter, multi-tag; no blur auto-commit). ✔
- Spec #3 (silence "thank you") → Tasks 8–10 (helpers, batch, live). ✔
- Spec #4 (speaker→person fails) → Tasks 6–7 (regex widen + real-error surfacing). ✔
- Spec #5 (system audio) → Tasks 11–12 (router verification + warning). ✔
- Out-of-scope (calendar, research) correctly excluded. ✔

**Placeholder scan:** No "TBD"/"handle edge cases"; every code step has concrete code. Three steps contain an explicit _confirm-then-adapt_ instruction (Task 3 auth helper name, Task 10 live config attribute, Task 12 orchestrator attribute names) because those exact identifiers were not captured verbatim in the map — each names the grep to run and the fallback, so they are actionable, not vague.

**Type consistency:** `setMeetingTags`/`getMeetingTags` (Task 4) match their uses in Task 5; `describeApiError(error, fallback)` (Task 7) matches its call sites; `get_distinct_tags` (Task 2) matches the route in Task 3; `_is_phrase_repetition`/`_is_known_hallucination_phrase`/`KNOWN_HALLUCINATION_PHRASES` (Task 8) match Tasks 9–10; `RoutingResult(error=...)` (Task 11) matches Task 12.
