# Action-Item Tags + Filtered Views Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag action items with a client/project (inherited from their meeting, overridable per item), and let the Action Items screen group and filter by client, project, status, priority, due-range, and assignee.

**Architecture:** Action items gain nullable `client_id`/`project_id` FKs plus a `tag_source` (`inherited`|`manual`) marker. Extraction inherits the meeting's client/project (`tag_source='inherited'`); a user override via `PATCH` sets `tag_source='manual'`. Reprocess deletes and re-extracts items but **preserves manually-tagged ones** (mirroring the existing "manual items are never touched" rule). Filtering is server-side (query params on `GET /api/action-items`); grouping is a client-side presentation over the filtered list.

**Tech Stack:** Python 3.12, FastAPI, `aiosqlite`, pytest; React 19 + TypeScript + TanStack Query + Vitest.

## Global Constraints

- **New columns:** `action_items.client_id` (TEXT, FK `clients(id)` `ON DELETE SET NULL`), `action_items.project_id` (TEXT, FK `projects(id)` `ON DELETE SET NULL`), `action_items.tag_source` (TEXT, default `'inherited'`, values `'inherited'` | `'manual'`). Copy these values verbatim.
- **Migration is v22.** The base branch is `main` at `SCHEMA_VERSION = 21` (PR #71 merged). Bump to 22; un-head the v21 block (its `PRAGMA user_version = {SCHEMA_VERSION}` becomes literal `21`) and add a new `if current_version < 22:` block. (SQLite `ALTER TABLE ADD COLUMN` cannot add a FK constraint to an existing table — the FK is declarative-only via `_safe_add_column`; that's acceptable and matches how `meetings.client_id`/`project_id` were added.)
- **Inheritance then override:** extracted items inherit the meeting's `client_id`/`project_id` with `tag_source='inherited'`; only a user `PATCH` sets `tag_source='manual'`.
- **Reprocess preserves manual tags:** `delete_extracted_for_meeting` must NOT delete rows with `tag_source='manual'`. (Trade-off: a manually-tagged extracted item survives reprocess and re-extraction may produce a fresh sibling — acceptable, documented; mirrors the existing keep-manual rule.)
- **Filtering server-side; grouping client-side.** `list_items` gains `client_id`, `project_id`, `priority`, `due_after` (plus existing `status`, `assignee`, `due_before`). Group-by (`project`|`client`|`status`|`due`|`meeting`) is done in the UI over the returned list.
- **macOS + Apple-Silicon only.** Python: `python3 -m pytest tests/`, `ruff check src/ tests/`. UI: `cd ui && npm test`, `npx tsc --noEmit`. Conventional-commit messages.

---

## File Structure

- **Modify** `src/db/database.py` — bump `SCHEMA_VERSION` to 22; add the three columns + indexes to the fresh-create path and a new `if current_version < 22:` block; un-head the v21 block.
- **Modify** `src/action_items/repository.py` — `create()` gains `client_id`/`project_id`/`tag_source` params; `_MUTABLE_COLUMNS` gains `client_id`/`project_id`/`tag_source`; `list_items` gains `client_id`/`project_id`/`priority`/`due_after`; `delete_extracted_for_meeting` preserves `tag_source='manual'`.
- **Modify** `src/pipeline_runner.py` (`_extract_action_items`, ~line 831) — fetch the meeting's client/project and pass them (with `tag_source='inherited'`) to `create()`.
- **Modify** `src/api/routes/action_items.py` — `UpdateActionItemRequest` gains `client_id`/`project_id`; the PATCH handler sets `tag_source='manual'` when either is provided; `list_action_items` gains the new query params.
- **Modify** `ui/src/lib/api.ts` — extend `getActionItems` (filters) and `updateActionItem` (client/project); **Modify** `ui/src/lib/types.ts` — `ActionItem` gains `client_id`/`project_id`/`tag_source`.
- **Modify** `ui/src/components/action-items/ActionItemList.tsx` — client/project/priority/due filter controls + a group-by selector.
- **Modify** `ui/src/components/action-items/ActionItemCard.tsx` — show the client/project tag and an inline client/project editor.

---

### Task 1: Migration v22 — action_items client/project/tag_source

**Files:**

- Modify: `src/db/database.py` (`SCHEMA_VERSION`, fresh-create path, v21 block, new v22 block)
- Modify: `src/db/repository.py` — n/a (action items have their own repo)
- Modify: `src/action_items/repository.py` (`create`, `_MUTABLE_COLUMNS`)
- Test: `tests/test_db_migration_v22.py` (new), `tests/test_action_item_repository.py` (extend — match its real name)

**Interfaces:**

- Consumes: nothing.
- Produces: `action_items.client_id` / `.project_id` (nullable) / `.tag_source` (default `'inherited'`); `ActionItemRepository.create(..., client_id=None, project_id=None, tag_source="inherited")`; `update()` accepts `client_id`/`project_id`/`tag_source`.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_v22.py` (mirror `tests/test_db_migration_v21.py`):

```python
"""v22 migration: add client_id / project_id / tag_source to action_items."""

import pytest

from src.db.database import Database


@pytest.mark.asyncio
async def test_v22_adds_action_item_tag_columns(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA table_info(action_items)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "client_id" in cols
        assert "project_id" in cols
        assert "tag_source" in cols

        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == 22
    finally:
        await db.close()
```

(Match `test_db_migration_v21.py`'s exact `Database(...)` construction — v21's test is the current template.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v22.py -v`
Expected: FAIL — columns absent / `user_version == 21`.

- [ ] **Step 3: Bump version + fresh-create path**

In `src/db/database.py`, set `SCHEMA_VERSION = 22`. In the fresh-create path, after the v21 (`title_source`/`markdown_path`) `_safe_add_column` calls, add:

```python
            # Action-item client/project tags (v22).
            await _safe_add_column(self.conn, "action_items", "client_id", "TEXT", "NULL")
            await _safe_add_column(self.conn, "action_items", "project_id", "TEXT", "NULL")
            await _safe_add_column(self.conn, "action_items", "tag_source", "TEXT", "'inherited'")
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_client ON action_items(client_id)"
            )
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_items_project ON action_items(project_id)"
            )
```

- [ ] **Step 4: Un-head v21 + add the v22 migration block**

In the `if current_version < 21:` block, change its final `await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")` to the literal:

```python
            await self.conn.execute("PRAGMA user_version = 21")
```

and after that block's `logger.info(... version 21 ...)` line add `current_version = 21`, then append a new block:

```python
        if current_version < 22:
            # Action-item client/project tags: items inherit their meeting's
            # client/project (tag_source='inherited'); a user override sets
            # tag_source='manual' and survives reprocess. Guard on the table
            # existing so minimal migration fixtures don't hard-fail.
            cur = await self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='action_items'"
            )
            if await cur.fetchone() is not None:
                await _safe_add_column(self.conn, "action_items", "client_id", "TEXT", "NULL")
                await _safe_add_column(self.conn, "action_items", "project_id", "TEXT", "NULL")
                await _safe_add_column(
                    self.conn, "action_items", "tag_source", "TEXT", "'inherited'"
                )
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_action_items_client "
                    "ON action_items(client_id)"
                )
                await self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_action_items_project "
                    "ON action_items(project_id)"
                )
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 22 (action-item tags)")
            current_version = 22
```

- [ ] **Step 5: Extend the action-item repository model**

In `src/action_items/repository.py`, add to `_MUTABLE_COLUMNS`:

```python
        "client_id",
        "project_id",
        "tag_source",
```

Extend `create()` — add params and columns. Change the signature (after `extracted_text`):

```python
        extracted_text: str | None = None,
        client_id: str | None = None,
        project_id: str | None = None,
        tag_source: str = "inherited",
    ) -> str:
```

and add the three columns to the INSERT column list + `VALUES` placeholders + the params tuple (after `extracted_text`):

```python
                INSERT INTO action_items
                    (id, meeting_id, title, description, assignee, status, priority,
                     due_date, reminder_at, source, extracted_text,
                     client_id, project_id, tag_source,
                     created_at, updated_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

and in the params tuple, insert `client_id, project_id, tag_source,` after `extracted_text,` and before `now,` (keep the ordering aligned with the column list).

- [ ] **Step 6: Add a repository test**

Add to the action-item repository test file (read it to confirm its name — likely `tests/test_action_item_repository.py`; use its existing DB fixture):

```python
@pytest.mark.asyncio
async def test_create_and_update_tags(action_item_repo):
    repo = action_item_repo
    item_id = await repo.create(
        meeting_id="m1", title="Ship it", client_id="c1", project_id="p1"
    )
    item = await repo.get(item_id)
    assert item["client_id"] == "c1"
    assert item["project_id"] == "p1"
    assert item["tag_source"] == "inherited"

    await repo.update(item_id, client_id="c2", tag_source="manual")
    item = await repo.get(item_id)
    assert item["client_id"] == "c2"
    assert item["tag_source"] == "manual"
```

(Match the real fixture name from the file's top; if it constructs a DB inline, mirror that.)

- [ ] **Step 7: Run tests + migration-adjacent suite**

Run: `python3 -m pytest tests/test_db_migration_v22.py tests/test_action_item_repository.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/db/database.py src/action_items/repository.py tests/test_db_migration_v22.py tests/test_action_item_repository.py
git commit -m "feat(db): add action_items client_id/project_id/tag_source (migration v22)"
```

---

### Task 2: Inherit tags on extraction + preserve manual tags on reprocess

**Files:**

- Modify: `src/pipeline_runner.py` (`_extract_action_items`, ~lines 831-861)
- Modify: `src/action_items/repository.py` (`delete_extracted_for_meeting`, ~line 139)
- Test: `tests/test_pipeline_runner.py`, `tests/test_action_item_repository.py`

**Interfaces:**

- Consumes: `ActionItemRepository.create(..., client_id=, project_id=, tag_source=)` (Task 1); the meeting row's `client_id`/`project_id` via `self._db.repo.get_meeting(meeting_id)`.
- Produces: extracted items carry the meeting's client/project (`tag_source='inherited'`); `delete_extracted_for_meeting` deletes only `source='extracted' AND tag_source != 'manual'`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_action_item_repository.py`:

```python
@pytest.mark.asyncio
async def test_delete_extracted_preserves_manual_tags(action_item_repo):
    repo = action_item_repo
    keep = await repo.create(meeting_id="m1", title="tagged", source="extracted")
    await repo.update(keep, client_id="c1", tag_source="manual")
    drop = await repo.create(meeting_id="m1", title="plain", source="extracted")

    deleted = await repo.delete_extracted_for_meeting("m1")
    assert deleted == 1
    assert await repo.get(keep) is not None
    assert await repo.get(drop) is None
```

Add to `tests/test_pipeline_runner.py` a test that a fresh extraction inherits the meeting's client/project. Match the file's runner/DbBridge harness; the meeting fetch is `repo.get_meeting`, which the fake repo must return with `client_id`/`project_id`:

```python
def test_extracted_action_items_inherit_meeting_client_project(tmp_path, loop_thread):
    repo = _make_repo()
    repo.get_meeting = AsyncMock(
        return_value=_meeting_record(client_id="c1", project_id="p1")
    )
    bridge = DbBridge(repo, loop_thread)
    config = _make_config(tmp_path)
    config.action_items.auto_extract = True
    runner = _make_runner(
        config, db=bridge,
        transcriber=FakeTranscriber(transcript=_make_transcript(("do the thing",))),
    )
    # Stub the extractor to yield one item and capture the repo.create kwargs.
    with patch("src.action_items.extractor.ActionItemExtractor") as Extr, \
         patch("src.action_items.repository.ActionItemRepository") as Repo:
        Extr.return_value.extract.return_value = [{"title": "Do the thing"}]
        ai_repo = Repo.return_value
        ai_repo.create = AsyncMock()
        ai_repo.delete_extracted_for_meeting = AsyncMock()
        runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)
        _drain(loop_thread)
        kwargs = ai_repo.create.await_args.kwargs
        assert kwargs["client_id"] == "c1"
        assert kwargs["project_id"] == "p1"
        assert kwargs["tag_source"] == "inherited"
```

`_meeting_record(...)` builds a `MeetingRecord` (or a MagicMock with `.client_id`/`.project_id`) — use whatever `get_meeting` returns in the real code (`MeetingRecord`); add a tiny helper if the file lacks one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_action_item_repository.py -k preserves_manual tests/test_pipeline_runner.py -k inherit -v`
Expected: FAIL — delete removes the tagged row; create receives no client/project.

- [ ] **Step 3: Preserve manual tags in delete_extracted_for_meeting**

In `src/action_items/repository.py`, change the DELETE:

```python
            cursor = await self._db.conn.execute(
                "DELETE FROM action_items "
                "WHERE meeting_id = ? AND source = 'extracted' "
                "AND (tag_source IS NULL OR tag_source != 'manual')",
                (meeting_id,),
            )
```

(`tag_source IS NULL` keeps pre-v22 rows deletable.)

- [ ] **Step 4: Inherit the meeting's client/project at extraction**

In `src/pipeline_runner.py::_extract_action_items`, before the `for item in items:` loop, fetch the meeting's tags, and pass them to `create()`:

```python
        meeting = await self._db.repo.get_meeting(meeting_id)
        m_client = getattr(meeting, "client_id", None) if meeting else None
        m_project = getattr(meeting, "project_id", None) if meeting else None
        for item in items:
            await ai_repo.create(
                meeting_id=meeting_id,
                title=item["title"],
                assignee=item.get("assignee"),
                due_date=item.get("due_date"),
                priority=item.get("priority", "medium"),
                source="extracted",
                extracted_text=item.get("extracted_text"),
                client_id=m_client,
                project_id=m_project,
                tag_source="inherited",
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_action_item_repository.py tests/test_pipeline_runner.py -k "preserves_manual or inherit or action_item" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline_runner.py src/action_items/repository.py tests/test_pipeline_runner.py tests/test_action_item_repository.py
git commit -m "feat(action-items): inherit meeting client/project on extract; preserve manual tags on reprocess"
```

---

### Task 3: Per-item tag override via PATCH

**Files:**

- Modify: `src/api/routes/action_items.py` (`UpdateActionItemRequest`, `update_action_item`)
- Test: `tests/test_api_action_items.py` (match its real name)

**Interfaces:**

- Consumes: `ActionItemRepository.update(item_id, client_id=, project_id=, tag_source=)` (Task 1).
- Produces: `PATCH /api/action-items/{id}` accepts `client_id`/`project_id`; when either is present, the row's `tag_source` becomes `'manual'`.

- [ ] **Step 1: Write the failing test**

Add to the action-items API test file:

```python
@pytest.mark.asyncio
async def test_patch_sets_client_project_and_marks_manual(action_items_client):
    client, repo = action_items_client
    item_id = await repo.create(meeting_id="m1", title="Do it", source="extracted")

    resp = client.patch(f"/api/action-items/{item_id}", json={"client_id": "c9"})
    assert resp.status_code == 200
    assert resp.json()["client_id"] == "c9"

    item = await repo.get(item_id)
    assert item["client_id"] == "c9"
    assert item["tag_source"] == "manual"
```

(Match the file's app/fixture pattern; if there's no such fixture, build a `FastAPI()` with `action_items_routes.init(repo)` + `TestClient`, returning `(client, repo)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_action_items.py -k patch_sets_client -v`
Expected: FAIL — `client_id` not accepted / `tag_source` not `manual`.

- [ ] **Step 3: Extend the request model + handler**

In `src/api/routes/action_items.py`, add to `UpdateActionItemRequest`:

```python
    client_id: str | None = None
    project_id: str | None = None
```

and change `update_action_item` so a tag change marks the row manual:

```python
@router.patch("/api/action-items/{item_id}")
async def update_action_item(item_id: str, body: UpdateActionItemRequest):
    repo = _get_repo()
    if not await repo.get(item_id):
        raise HTTPException(status_code=404, detail="Action item not found")
    fields = body.model_dump(exclude_none=True)
    if "client_id" in fields or "project_id" in fields:
        fields["tag_source"] = "manual"
    if fields:
        await repo.update(item_id, **fields)
    return await repo.get(item_id)
```

(Note: `exclude_none=True` means a client can set a tag but cannot clear it to null through this path — matching the existing model's behaviour for other nullable fields; clearing a tag is out of scope.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_action_items.py -k patch_sets_client -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/action_items.py tests/test_api_action_items.py
git commit -m "feat(action-items): PATCH client/project per-item override (marks tag_source manual)"
```

---

### Task 4: Server-side filters on the list endpoint

**Files:**

- Modify: `src/action_items/repository.py` (`list_items`)
- Modify: `src/api/routes/action_items.py` (`list_action_items`)
- Test: `tests/test_action_item_repository.py`, `tests/test_api_action_items.py`

**Interfaces:**

- Consumes: the v22 columns.
- Produces: `list_items(status=, assignee=, due_before=, due_after=, client_id=, project_id=, priority=, limit=, offset=)`; `GET /api/action-items` accepts `client_id`, `project_id`, `priority`, `due_after` in addition to the existing params.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_action_item_repository.py`:

```python
@pytest.mark.asyncio
async def test_list_items_filters_by_client_and_priority(action_item_repo):
    repo = action_item_repo
    await repo.create(meeting_id="m1", title="a", priority="high", client_id="c1")
    await repo.create(meeting_id="m1", title="b", priority="low", client_id="c1")
    await repo.create(meeting_id="m1", title="c", priority="high", client_id="c2")

    got = await repo.list_items(client_id="c1", priority="high")
    assert [i["title"] for i in got] == ["a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_action_item_repository.py -k filters_by_client -v`
Expected: FAIL — `list_items` has no `client_id`/`priority` params.

- [ ] **Step 3: Extend `list_items`**

In `src/action_items/repository.py::list_items`, add the params to the signature (after `due_before`):

```python
        due_after: str | None = None,
        client_id: str | None = None,
        project_id: str | None = None,
        priority: str | None = None,
```

and add the conditions (after the existing `due_before` condition):

```python
        if due_after is not None:
            conditions.append("due_date > ?")
            params.append(due_after)
        if client_id is not None:
            conditions.append("client_id = ?")
            params.append(client_id)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        if priority is not None:
            conditions.append("priority = ?")
            params.append(priority)
```

- [ ] **Step 4: Extend the route**

In `src/api/routes/action_items.py::list_action_items`, add the query params + pass-through:

```python
@router.get("/api/action-items")
async def list_action_items(
    status: str | None = None,
    assignee: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    client_id: str | None = None,
    project_id: str | None = None,
    priority: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    items = await _get_repo().list_items(
        status=status,
        assignee=assignee,
        due_before=due_before,
        due_after=due_after,
        client_id=client_id,
        project_id=project_id,
        priority=priority,
        limit=limit,
        offset=offset,
    )
    return {"items": items}
```

- [ ] **Step 5: Add a route test + run**

Add to `tests/test_api_action_items.py`:

```python
@pytest.mark.asyncio
async def test_list_endpoint_filters_by_project(action_items_client):
    client, repo = action_items_client
    await repo.create(meeting_id="m1", title="x", project_id="p1")
    await repo.create(meeting_id="m1", title="y", project_id="p2")
    resp = client.get("/api/action-items?project_id=p1")
    assert resp.status_code == 200
    assert [i["title"] for i in resp.json()["items"]] == ["x"]
```

Run: `python3 -m pytest tests/test_action_item_repository.py tests/test_api_action_items.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/action_items/repository.py src/api/routes/action_items.py tests/test_action_item_repository.py tests/test_api_action_items.py
git commit -m "feat(action-items): server-side filters by client/project/priority/due-range"
```

---

### Task 5: UI API client + types

**Files:**

- Modify: `ui/src/lib/api.ts` (`getActionItems`, `updateActionItem`)
- Modify: `ui/src/lib/types.ts` (`ActionItem`)
- Test: covered by Task 6's component tests + tsc.

**Interfaces:**

- Consumes: the extended endpoints (Tasks 3–4).
- Produces: `getActionItems(opts?: { status?, assignee?, clientId?, projectId?, priority?, dueBefore?, dueAfter?, limit? })`; `updateActionItem(id, patch)` accepting `client_id`/`project_id`; `ActionItem` gains `client_id?`/`project_id?`/`tag_source?`.

- [ ] **Step 1: Extend the types**

In `ui/src/lib/types.ts`, add to `interface ActionItem` (after `extracted_text`):

```typescript
  client_id?: string | null;
  project_id?: string | null;
  tag_source?: string;
```

- [ ] **Step 2: Extend `getActionItems`**

In `ui/src/lib/api.ts`, replace `getActionItems` with an options object (read the current callers — `ActionItemList` calls `getActionItems(statusFilter || undefined)`; update that call in Task 6):

```typescript
export async function getActionItems(opts?: {
  status?: string;
  assignee?: string;
  clientId?: string;
  projectId?: string;
  priority?: string;
  dueBefore?: string;
  dueAfter?: string;
  limit?: number;
}): Promise<ActionItemsResponse> {
  const params = new URLSearchParams({ limit: String(opts?.limit ?? 100) });
  if (opts?.status) params.set("status", opts.status);
  if (opts?.assignee) params.set("assignee", opts.assignee);
  if (opts?.clientId) params.set("client_id", opts.clientId);
  if (opts?.projectId) params.set("project_id", opts.projectId);
  if (opts?.priority) params.set("priority", opts.priority);
  if (opts?.dueBefore) params.set("due_before", opts.dueBefore);
  if (opts?.dueAfter) params.set("due_after", opts.dueAfter);
  return request<ActionItemsResponse>(`/api/action-items?${params}`);
}
```

- [ ] **Step 3: Confirm `updateActionItem` accepts the tags**

Read `updateActionItem` in `ui/src/lib/api.ts`. If it takes a typed `patch` object, add `client_id?: string | null; project_id?: string | null;` to that param type. If it already forwards an arbitrary partial `ActionItem`, no change is needed — verify and note it.

- [ ] **Step 4: Type-check + commit**

Run: `cd ui && npx tsc --noEmit`
Expected: errors ONLY at the now-changed `getActionItems(statusFilter)` call site in `ActionItemList` (fixed in Task 6). If tsc fails elsewhere, fix the caller signatures. To keep this commit green on its own, also apply Task 6's one-line call-site update, or land Tasks 5+6 together.

```bash
git add ui/src/lib/api.ts ui/src/lib/types.ts
git commit -m "feat(ui): action-item filter params + client/project on the type"
```

---

### Task 6: Filter controls, group-by, and per-item tag editing

**Files:**

- Modify: `ui/src/components/action-items/ActionItemList.tsx`
- Modify: `ui/src/components/action-items/ActionItemCard.tsx`
- Test: `ui/src/components/action-items/__tests__/ActionItemList.test.tsx` (create/extend), `.../ActionItemCard.test.tsx`

**Interfaces:**

- Consumes: `getActionItems(opts)` (Task 5), `getClients`/`getProjects` (existing), `updateActionItem` (Task 5), `Client`/`Project` types.
- Produces: the Action Items screen filters by client/project/priority/status (server-side) and groups the returned list by a chosen dimension (`none`|`client`|`project`|`status`|`due`|`meeting`); each card shows and edits its client/project tag.

- [ ] **Step 1: Write the failing list test**

In `ui/src/components/action-items/__tests__/ActionItemList.test.tsx`, add a test that selecting a project filter refetches with `project_id`. Mock `fetch` to capture the URL; assert a request to `/api/action-items?...project_id=p1`. Follow the existing test idiom in this directory (`makeWrapper`, `fetch` stub). Also add a test that with group-by = "status", items render under status group headers.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- ActionItemList`
Expected: FAIL — no project filter / no group headers.

- [ ] **Step 3: Implement filters + group-by in ActionItemList**

Extend `ActionItemList.tsx`:

- Replace the `getActionItems(statusFilter || undefined)` call with the options form: `getActionItems({ status: statusFilter || undefined, clientId: clientFilter || undefined, projectId: projectFilter || undefined, priority: priorityFilter || undefined })`, and add `clientFilter`/`projectFilter`/`priorityFilter`/`groupBy` to `useState` and the `queryKey` array (`["action-items", statusFilter, clientFilter, projectFilter, priorityFilter]`).
- Fetch clients + projects for the dropdowns: `const { data: clients } = useQuery({ queryKey: ["clients"], queryFn: () => getClients() })` and likewise `getProjects()`.
- Render `<select>` controls for client, project, priority (alongside the existing status filter), and a group-by `<select>` (`none|client|project|status|due|meeting`).
- Group the returned `items` client-side: a `groupItems(items, groupBy, {clients, projects})` helper returning `[{ key, label, items }]`; render a header per group. For `groupBy === "none"`, one unlabelled group. For `client`/`project`, resolve the id → name via the fetched lists (fall back to "Unassigned" for null). For `due`, bucket by overdue / today / this-week / later / no-date. For `meeting`, group by `meeting_id`.

Provide the complete `groupItems` helper and the JSX in the implementation (this is the bulk of the task — write it against the real component structure you read in Step 0).

- [ ] **Step 4: Implement the tag display + editor in ActionItemCard**

Extend `ActionItemCard.tsx`: show the item's client/project name (resolved from a passed-in lookup or a small `useQuery(["clients"])`/`["projects"]`), and add an inline client/project `<select>` pair that calls `updateActionItem(item.id, { client_id, project_id })` on change and invalidates `["action-items"]`. Keep it compact (a small "Tag" affordance) so the card stays readable.

- [ ] **Step 5: Run the UI tests + type-check**

Run: `cd ui && npm test -- ActionItem && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/action-items/ActionItemList.tsx ui/src/components/action-items/ActionItemCard.tsx ui/src/components/action-items/__tests__
git commit -m "feat(ui): action-item client/project filters, group-by, and per-item tagging"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass. If a pre-existing action-item test asserts an exact row/`to_dict` shape, update it to include the new `client_id`/`project_id`/`tag_source` keys.

- [ ] **Step 2: Python lint**

Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 3: UI tests + type-check**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit any verification fixes**

```bash
git add -A
git commit -m "test(action-items): fix assertions surfaced by client/project tags"
```

---

## Manual verification (not automatable in CI)

1. Record (or reprocess) a meeting assigned to a client/project → its extracted action items show that client/project tag.
2. Change one item's tag to a different project → it moves under that project's group; reprocess the meeting → the manual tag survives (the item is not wiped).
3. On the Action Items screen, filter by client and priority → the list narrows server-side; switch group-by between project/client/status/due/meeting → the same items regroup.

---

## Self-Review

- **Spec coverage:** migration adding nullable `client_id`/`project_id` FK `ON DELETE SET NULL` + indexes → Task 1 (plus `tag_source` for reprocess-safety); inherit meeting's client/project on extraction → Task 2; `PATCH` per-item override → Task 3; reprocess replace must not wipe a manual tag → Task 2 (`delete_extracted_for_meeting` preserves `tag_source='manual'`); server-side filters (client, project, status, priority, due-range, assignee) on `GET /api/action-items` → Task 4 (assignee/status/due_before already existed; added client/project/priority/due_after); group-by (project/client/status/due/meeting) → Task 6 (client-side over the filtered list). Migration is v22 (base main @ v21).
- **Placeholder scan:** none — backend steps carry complete code; the two UI steps (Task 6 `groupItems` + the card editor) name the exact components, existing helpers (`getClients`/`getProjects`/`updateActionItem`/`AssignmentSelect` pattern), and behaviours to implement against the real files, because the surrounding component structure must be read and matched rather than invented.
- **Type consistency:** `create(..., client_id, project_id, tag_source)` and `update`'s `_MUTABLE_COLUMNS` (Task 1) are used identically in Tasks 2–3; `list_items(... client_id, project_id, priority, due_after)` (Task 4) matches the route params; `getActionItems(opts)` and the `ActionItem` tag fields (Task 5) are consumed in Task 6; `tag_source` values `'inherited'`/`'manual'` are consistent across Tasks 1–3.
