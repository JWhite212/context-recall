# Notifications Redesign (Phase 0 + 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the 99+ notification pileup at its source and turn the feature into a correct, controllable, one-tap-manageable notifications system — without yet adding new event types (that is Phase 2).

**Architecture:** Every notification flows through a single governed `NotificationDispatcher.notify()` chokepoint that applies six gates in order — master/per-type mute → DB-level dedup → rate limits → priority→channel routing → quiet hours → deliver-and-persist. Notifications become one persistent row per logical event (not per channel) with a real `unread → read → dismissed → pruned` lifecycle. Producers (overdue/reminder sweeps, automations) are rewritten to notify on _state change_ instead of re-firing every sweep, and a retention task keeps the table bounded.

**Tech Stack:** Python 3 (async, `aiosqlite`), FastAPI, a SQLite database with `PRAGMA user_version` migrations; React + TypeScript + Tauri frontend with `@tanstack/react-query`; pytest + pytest-asyncio (backend) and vitest + @testing-library/react (frontend).

## Global Constraints

- **No new third-party dependencies** — backend or frontend.
- **Schema migration:** `SCHEMA_VERSION` goes `24 → 25` in `src/db/database.py`. Migrations are applied by version in `_migrate()` (`if current_version == 24: ... PRAGMA user_version = 25`). SQLite cannot `ADD COLUMN` with `UNIQUE`, so add plain columns and create a separate `UNIQUE INDEX`.
- **Status vocabulary:** `unread | read | dismissed | failed`. The badge counts `unread` and not-snoozed only.
- **One row per logical event** — the delivered channel set is stored as a JSON string in `channels`; the legacy `channel` column stays `NOT NULL` and is set to `channels[0]` (or `"in_app"`).
- **Dedup key** default: `f"{type}:{reference_id}:{int(now // 86400)}"`; enforced by a `UNIQUE INDEX` on `dedup_key` (SQLite treats `NULL`s as distinct) via `INSERT … ON CONFLICT(dedup_key) DO NOTHING`.
- **Defaults (product-owner approved):** overdue → notify once on transition + daily digest (never hourly); retention keep 30 days, prune dismissed after 7, hard cap 500 rows; quiet hours `22:00–08:00`; macOS sound **off** by default.
- **TDD:** write the failing test first, watch it fail, implement minimally, watch it pass, commit. Keep both suites green.
- **Commits:** conventional commits. **No AI/Claude attribution of any kind** — no session trailer, no `Co-Authored-By`, no "Generated with" line.
- **Follow existing patterns:** async DB access via `self._db.conn.execute(...)` + `await self._db.conn.commit()`; timestamps `time.time()`; ids `str(uuid.uuid4())`. Mirror the test style in `tests/test_notifications.py` and `ui/src/components/**/__tests__`.
- **Phasing:** Phase 0 (Tasks 1–2) is independently mergeable and must not depend on the v25 migration. Phase 1 (Tasks 3–17) builds on it. Phase 2 (new events, inbox grouping/click-through, snooze/mark-done UI, surface unification) is a **separate follow-up plan**.

---

## Execution Reconciliation Notes

Read before executing — these reconcile cross-task ordering and a few conscious design calls surfaced during plan review:

1. **Phase 0 → Phase 1 storage-rewrite window.** Phase 0 (Tasks 1–2) targets the live v24 schema and is independently mergeable. Phase 1 Tasks 3–4 rewrite the storage layer (v25 migration + `create`), which necessarily supersedes some Phase-0-era code: Task 4 deletes the legacy `dispatcher` fixture + 3 legacy dispatcher tests in `tests/test_notifications.py`; Task 5 deletes the Phase-0 `dismiss_all` and replaces it with the v25 version (the two must not coexist); Task 14 replaces the Phase-0 `tests/test_api_notifications.py` with v25-aware coverage. Run each task's scoped test command; the full `tests/test_notifications.py` is green from Task 5 onward.
2. **macOS volume cap (spec 5.2 gate 3).** There is no dedicated `macos_max_per_hour`. macOS banner volume is bounded _by design_ through the global/per-type `max_per_hour` + `macos_min_priority` routing + quiet hours + sound-off — a conscious choice to avoid an extra knob. To add per-channel macOS throttling later: a `macos_max_per_hour` field (Task 7) plus a `count_channel_since("macos", now - 3600)` gate in the dispatcher (Task 9).
3. **Migration ladder style.** The existing `_migrate()` uses `if current_version < N:` steps (only the oldest uses `== 2`). Add the v25 step as `if current_version < 25:` immediately after the `< 24` block — functionally equivalent to the `== 24` guard shown in Task 3 (the `< 24` block leaves `current_version == 24`).
4. **`notif_repo` is file-local.** It lives in `tests/test_notifications.py` (not `conftest.py`, which provides `db`, `event_bus`, `repo`, plus pytest's `tmp_path`). Tasks 4/5/6 append to that file, so it's in scope; any _new_ test file needing it (e.g. Task 9's `tests/test_notifications_dispatcher.py`) must define its own.

---

## Phase 0 — Immediate relief (independently mergeable; no v25 dependency)

### Task 1: Silence the macOS banner by default (make sound opt-in)

**Files:**

- Modify `src/notifications/channels/macos.py:9` — change `send()` signature to `async def send(title: str, body: str, *, sound: bool = False, subtitle: str = "") -> bool`, gate the `sound name "default"` line on `sound`, and return `True` only when `osascript` returncode is 0.
- Test (new): `tests/test_macos_channel.py`.
- No change to `src/notifications/dispatcher.py:103` or `src/automations/executor.py:126` — both call `send(title, body)` positionally, so with the new default `sound=False` every existing banner is silent immediately. (Note this explicitly in the commit body.)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: `channels/macos.send(title: str, body: str, *, sound: bool = False, subtitle: str = "") -> bool` — the exact locked signature the Phase 2 dispatcher rebuild will call with `sound=self._config.macos_sound`. Returns `True` iff `osascript` exits 0.

- [ ] **Step 1: Write the failing test** — create `tests/test_macos_channel.py`:

```python
"""Tests for src/notifications/channels/macos.py — sound is opt-in."""

import pytest

from src.notifications.channels import macos


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self):
        return (b"", b"")


def _capture_exec(monkeypatch, returncode: int = 0) -> dict:
    """Patch osascript exec; capture the args it would be invoked with."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(returncode=returncode)

    monkeypatch.setattr(macos.asyncio, "create_subprocess_exec", fake_exec)
    return captured


@pytest.mark.asyncio
async def test_send_omits_sound_by_default(monkeypatch):
    captured = _capture_exec(monkeypatch)
    result = await macos.send("Standup", "Notes are ready")
    assert result is True
    script = captured["args"][2]  # ("osascript", "-e", <script>)
    assert "sound name" not in script


@pytest.mark.asyncio
async def test_send_includes_sound_when_enabled(monkeypatch):
    captured = _capture_exec(monkeypatch)
    result = await macos.send("Standup", "Notes are ready", sound=True)
    assert result is True
    script = captured["args"][2]
    assert 'sound name "default"' in script


@pytest.mark.asyncio
async def test_send_returns_false_on_nonzero_returncode(monkeypatch):
    _capture_exec(monkeypatch, returncode=1)
    result = await macos.send("Standup", "Notes are ready")
    assert result is False
```

- [ ] **Step 2: Run test to verify it fails**
  - Command: `python -m pytest tests/test_macos_channel.py -q`
  - Expected failure: `test_send_omits_sound_by_default` fails on `assert "sound name" not in script` (current code always appends it); `test_send_includes_sound_when_enabled` errors with `TypeError: send() got an unexpected keyword argument 'sound'`; `test_send_returns_false_on_nonzero_returncode` fails because current `send()` returns `None`, not `False`.

- [ ] **Step 3: Write minimal implementation** — replace the full contents of `src/notifications/channels/macos.py` with:

```python
"""macOS native notification via osascript."""

import asyncio
import logging

logger = logging.getLogger("contextrecall.notifications.macos")


async def send(title: str, body: str, *, sound: bool = False, subtitle: str = "") -> bool:
    """Display a macOS banner. Sound is opt-in. Returns True iff osascript exits 0."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_subtitle = subtitle.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    if safe_subtitle:
        script += f' subtitle "{safe_subtitle}"'
    if sound:
        script += ' sound name "default"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("osascript failed: %s", stderr.decode())
            return False
        return True
    except Exception as e:
        logger.warning("macOS notification failed: %s", e)
        return False
```

- [ ] **Step 4: Run test to verify it passes**
  - Command: `python -m pytest tests/test_macos_channel.py -q`
  - Expected: `3 passed`. Sanity-check nothing else broke: `python -m pytest tests/test_notifications.py -q` → still passes (dispatcher call `macos.send(title, body)` remains valid; banner now silent).

- [ ] **Step 5: Commit**

```
git add src/notifications/channels/macos.py tests/test_macos_channel.py
git commit -m "feat(notifications): make macOS banner sound opt-in

send() gains a keyword-only sound flag (default False) and only emits the
osascript 'sound name \"default\"' line when it is set, and now returns True
only on a zero osascript return code. Both existing callers pass no sound, so
banners go silent immediately. Phase 0 relief; no schema dependency."
```

---

### Task 2: Purge the backlog + expose bulk clear (v24-compatible)

**Files:**

- Modify `src/notifications/repository.py` — append `dismiss_all` at end of file, after the existing `dismiss` method (which ends at line 147). Operates on the CURRENT v24 schema (`status='sent'` is the unread backlog). **Task 5 later deletes this Phase-0 `dismiss_all` and replaces it with the v25 version** — the two must not coexist.
- Modify `src/api/routes/notifications.py` — add a `ClearAllRequest` model and a `POST /api/notifications/clear-all` route (current file ends at line 42).
- Test (new): `tests/test_api_notifications.py` (covers both the repo method and the endpoint via one fixture, mirroring `tests/test_api_action_items.py`).

**Interfaces:**

- Consumes: `NotificationRepository.count_unread()` and `create(type, title, body, channel, status=...)` (existing v24 signatures); `notifications_routes.init(repo)` / `.router` (existing).
- Produces: `NotificationRepository.dismiss_all(self, *, type: str | None = None) -> int` (rows affected) — the exact locked signature Phase 1 later re-points at the v25 status vocabulary; and `POST /api/notifications/clear-all` body `{type?: str}` → `{updated: int}`, the endpoint the Phase 1 UI (`clearAllNotifications`) calls.

- [ ] **Step 1: Write the failing test** — create `tests/test_api_notifications.py`:

```python
"""Tests for bulk clear-all — repo method + endpoint (v24 schema)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import notifications as notifications_routes
from src.db.database import Database
from src.notifications.repository import NotificationRepository


@pytest.fixture
async def notifications_client(tmp_path):
    db = Database(db_path=tmp_path / "notifications_api.db")
    await db.connect()
    repo = NotificationRepository(db)
    notifications_routes.init(repo)

    app = FastAPI()
    app.include_router(notifications_routes.router)
    with TestClient(app) as client:
        yield client, repo
    await db.close()


@pytest.mark.asyncio
async def test_dismiss_all_dismisses_sent_backlog(notifications_client):
    _client, repo = notifications_client
    await repo.create(type="overdue", title="A", body="", channel="in_app", status="sent")
    await repo.create(type="overdue", title="B", body="", channel="macos", status="sent")
    await repo.create(type="reminder", title="C", body="", channel="in_app", status="sent")
    # Already-dismissed rows must not be counted or re-touched.
    await repo.create(type="overdue", title="D", body="", channel="in_app", status="dismissed")

    updated = await repo.dismiss_all()
    assert updated == 3
    assert await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_dismiss_all_type_filter(notifications_client):
    _client, repo = notifications_client
    await repo.create(type="overdue", title="A", body="", channel="in_app", status="sent")
    await repo.create(type="reminder", title="B", body="", channel="in_app", status="sent")

    updated = await repo.dismiss_all(type="overdue")
    assert updated == 1
    remaining = await repo.list_notifications(limit=10, status="sent")
    assert len(remaining) == 1
    assert remaining[0]["type"] == "reminder"


@pytest.mark.asyncio
async def test_clear_all_endpoint_purges_backlog(notifications_client):
    client, repo = notifications_client
    await repo.create(type="overdue", title="A", body="", channel="in_app", status="sent")
    await repo.create(type="overdue", title="B", body="", channel="macos", status="sent")

    resp = client.post("/api/notifications/clear-all", json={})
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2}
    assert await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_clear_all_endpoint_type_filter(notifications_client):
    client, repo = notifications_client
    await repo.create(type="overdue", title="A", body="", channel="in_app", status="sent")
    await repo.create(type="reminder", title="B", body="", channel="in_app", status="sent")

    resp = client.post("/api/notifications/clear-all", json={"type": "overdue"})
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1}
    assert await repo.count_unread() == 1
```

- [ ] **Step 2: Run test to verify it fails**
  - Command: `python -m pytest tests/test_api_notifications.py -q`
  - Expected failure: the `dismiss_all` tests raise `AttributeError: 'NotificationRepository' object has no attribute 'dismiss_all'`; the endpoint tests fail on `assert resp.status_code == 200` (route returns `404 Not Found`).

- [ ] **Step 3: Write minimal implementation**

  In `src/notifications/repository.py`, append this method to the `NotificationRepository` class (immediately after `dismiss`, i.e. at end of file after line 147):

```python
    async def dismiss_all(self, *, type: str | None = None) -> int:
        """Bulk-dismiss the unread backlog (v24 status='sent'); return rows updated.

        Optionally scoped to a single notification type. Used by the Phase 0
        one-time purge and the /clear-all endpoint. v24-compatible: it flips the
        legacy 'sent' status to 'dismissed' and leaves 'dismissed'/'failed' as-is.
        """
        if type is not None:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status = 'dismissed' "
                "WHERE status = 'sent' AND type = ?",
                (type,),
            )
        else:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status = 'dismissed' WHERE status = 'sent'"
            )
        await self._db.conn.commit()
        return cursor.rowcount
```

In `src/api/routes/notifications.py`, add the request model next to `DismissRequest` (after line 24) and the route at the end of the file (after line 42):

```python
class ClearAllRequest(BaseModel):
    type: str | None = None
```

```python
@router.post("/clear-all")
async def clear_all(body: ClearAllRequest | None = None):
    type_filter = body.type if body is not None else None
    updated = await _get_repo().dismiss_all(type=type_filter)
    return {"updated": updated}
```

- [ ] **Step 4: Run test to verify it passes**
  - Command: `python -m pytest tests/test_api_notifications.py tests/test_notifications.py -q`
  - Expected: all tests pass (`4 passed` for the new file; existing notifications suite unchanged).

- [ ] **Step 5: Commit**

```
git add src/notifications/repository.py src/api/routes/notifications.py tests/test_api_notifications.py
git commit -m "feat(notifications): add bulk clear-all endpoint and dismiss_all repo method

NotificationRepository.dismiss_all(*, type=None) flips the unread backlog
(v24 status='sent') to 'dismissed' and returns rows affected; POST
/api/notifications/clear-all exposes it with an optional {type} filter. Lets
the user drain the 99+ backlog in one tap. v24-compatible; no v25 dependency."
```

## Phase 1 — Streamline & make correct

### Task 3: Schema v25 migration (notifications lifecycle + action_items transition column)

**Files:**

- Modify `src/db/database.py:26` (bump `SCHEMA_VERSION`), `src/db/database.py:198-215` (`NOTIFICATIONS_SQL` for fresh installs), `src/db/database.py:995-1001` (add the `if current_version == 24:` migration block, moving the trailing `else`).
- Create `tests/test_db_migration_v25.py`.

**Interfaces:** Consumes: the existing `Database.connect()` → `_migrate()` ladder and the fresh-install `if current_version < 1:` path (which runs `NOTIFICATIONS_SQL`). Produces: a `notifications` table carrying `read_at, dedup_key, group_key, reference_type, priority, channels, snoozed_until` + indexes `idx_notifications_created_at` and unique `idx_notifications_dedup`, an `action_items.overdue_notified_at` column, and `PRAGMA user_version = 25` — the storage substrate every later task (Repository create/dedup, count_unread snooze filter, retention prune) relies on.

- [ ] **Step 1: Write the failing test** — create `tests/test_db_migration_v25.py`:

```python
"""Forward-migration test for schema v25 (notifications redesign)."""

import aiosqlite
import pytest

from src.db.database import Database

# The v24-era notifications table (pre-redesign shape).
_V24_NOTIFICATIONS_SQL = """
CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    reference_id TEXT,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at REAL,
    sent_at REAL,
    created_at REAL NOT NULL
);
"""

# Minimal v24 action_items (includes due_date/status so the idempotent
# idx_action_items_due_status index build is a clean no-op).
_V24_ACTION_ITEMS_SQL = """
CREATE TABLE action_items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    due_date TEXT,
    reminder_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


async def _make_v24_db(path):
    conn = await aiosqlite.connect(str(path))
    await conn.executescript(_V24_NOTIFICATIONS_SQL)
    await conn.executescript(_V24_ACTION_ITEMS_SQL)
    # A delivered ('sent') row that must backfill to 'unread', and a
    # 'dismissed' row that must be left untouched.
    await conn.execute(
        "INSERT INTO notifications (id, type, channel, title, body, status, created_at) "
        "VALUES ('n1', 'reminder', 'in_app', 'Old', 'body', 'sent', 100.0)"
    )
    await conn.execute(
        "INSERT INTO notifications (id, type, channel, title, body, status, created_at) "
        "VALUES ('n2', 'reminder', 'macos', 'Old2', 'body', 'dismissed', 100.0)"
    )
    await conn.execute("PRAGMA user_version = 24")
    await conn.commit()
    await conn.close()


async def _columns(db, table):
    cur = await db.conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _indexes(db, table):
    cur = await db.conn.execute(f"PRAGMA index_list({table})")
    return {row[1] for row in await cur.fetchall()}


_NEW_NOTIF_COLS = (
    "read_at", "dedup_key", "group_key", "reference_type",
    "priority", "channels", "snoozed_until",
)


@pytest.mark.asyncio
async def test_v24_migrates_to_v25(tmp_path):
    path = tmp_path / "v24.db"
    await _make_v24_db(path)

    db = Database(db_path=path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 25

        cols = await _columns(db, "notifications")
        for c in _NEW_NOTIF_COLS:
            assert c in cols, f"missing notifications column {c}"

        idx = await _indexes(db, "notifications")
        assert "idx_notifications_created_at" in idx
        assert "idx_notifications_dedup" in idx

        assert "overdue_notified_at" in await _columns(db, "action_items")

        # Backfill: delivered 'sent' -> 'unread'; 'dismissed' preserved.
        cur = await db.conn.execute("SELECT status FROM notifications WHERE id='n1'")
        assert (await cur.fetchone())[0] == "unread"
        cur = await db.conn.execute("SELECT status FROM notifications WHERE id='n2'")
        assert (await cur.fetchone())[0] == "dismissed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_db_has_v25_schema(db):
    cur = await db.conn.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == 25

    cols = await _columns(db, "notifications")
    for c in _NEW_NOTIF_COLS:
        assert c in cols, f"missing notifications column {c}"

    idx = await _indexes(db, "notifications")
    assert "idx_notifications_created_at" in idx
    assert "idx_notifications_dedup" in idx

    assert "overdue_notified_at" in await _columns(db, "action_items")
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_db_migration_v25.py -q
```

Expected failure: both tests fail — `test_fresh_db_has_v25_schema` asserts `user_version == 25` but gets `24`; `test_v24_migrates_to_v25` fails at the same assert (the `if current_version == 24:` block does not yet exist and the new columns are absent).

- [ ] **Step 3: Write minimal implementation** — three edits in `src/db/database.py`:

(a) Bump the version constant (line 26):

```python
SCHEMA_VERSION = 25
```

(b) Replace the fresh-install `NOTIFICATIONS_SQL` block (lines 198-215) so new databases are born at v25:

```python
NOTIFICATIONS_SQL = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    reference_id TEXT,
    reference_type TEXT,
    channel TEXT NOT NULL,
    channels TEXT,
    title TEXT NOT NULL,
    body TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'unread',
    dedup_key TEXT,
    group_key TEXT,
    scheduled_at REAL,
    sent_at REAL,
    read_at REAL,
    snoozed_until REAL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications(type);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notifications_reference ON notifications(reference_id);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedup ON notifications(dedup_key);
"""
```

(c) At the tail of `_migrate` (lines 995-1001), replace:

```python
            logger.info("Database migrated to version 24 (recording↔calendar link)")
            current_version = 24
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

with:

```python
            logger.info("Database migrated to version 24 (recording↔calendar link)")
            current_version = 24

        if current_version == 24:
            # Notifications redesign (v25): one-row-per-event lifecycle, DB-level
            # dedup, retention indexing. SQLite cannot ADD COLUMN with UNIQUE, so
            # add plain columns then a separate unique index. Each ALTER is guarded
            # on the column being absent so a fresh install (whose NOTIFICATIONS_SQL
            # already carries these columns) re-entering this block is a safe no-op.
            cur = await self.conn.execute("PRAGMA table_info(notifications)")
            notif_cols = {row[1] for row in await cur.fetchall()}
            new_notification_columns = (
                ("read_at", "REAL"),
                ("dedup_key", "TEXT"),
                ("group_key", "TEXT"),
                ("reference_type", "TEXT"),
                ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
                ("channels", "TEXT"),
                ("snoozed_until", "REAL"),
            )
            for name, decl in new_notification_columns:
                if name not in notif_cols:
                    await self.conn.execute(
                        f"ALTER TABLE notifications ADD COLUMN {name} {decl}"
                    )
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_created_at "
                "ON notifications(created_at)"
            )
            # Partial-unique semantics: SQLite treats NULLs as distinct, so many
            # NULL dedup_keys coexist while non-null keys are unique.
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedup "
                "ON notifications(dedup_key)"
            )
            # Backfill legacy status: delivered rows become 'unread'; leave
            # 'dismissed'/'failed' as-is.
            await self.conn.execute(
                "UPDATE notifications SET status='unread' WHERE status='sent'"
            )
            # action_items transition tracking so overdue notifies once. Guard on
            # the table existing (mirrors the v21/v22/v24 defensive checks).
            cur = await self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='action_items'"
            )
            if await cur.fetchone() is not None:
                cur = await self.conn.execute("PRAGMA table_info(action_items)")
                ai_cols = {row[1] for row in await cur.fetchall()}
                if "overdue_notified_at" not in ai_cols:
                    await self.conn.execute(
                        "ALTER TABLE action_items ADD COLUMN overdue_notified_at REAL"
                    )
            await self.conn.execute("PRAGMA user_version = 25")
            await self.conn.commit()
            logger.info("Database migrated to version 25 (notifications redesign)")
            current_version = 25
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_db_migration_v25.py -q
```

Expected: `2 passed`. (Fresh DBs walk the ladder to `current_version == 24`, then the guarded block advances them to 25 with no-op ALTERs; a real v24 DB migrates with live ALTERs and the `'sent'→'unread'` backfill.)

- [ ] **Step 5: Commit**

```
git add src/db/database.py tests/test_db_migration_v25.py
git commit -m "feat(db): add schema v25 migration for notifications lifecycle

Add read_at/dedup_key/group_key/reference_type/priority/channels/
snoozed_until columns, created_at index, unique dedup index, and
action_items.overdue_notified_at. Backfill legacy status 'sent'->'unread'.
Update NOTIFICATIONS_SQL so fresh installs are born at v25."
```

---

### Task 4: `NotificationRepository.create` rewrite (one row per event, channels JSON, DB-level dedup)

**Files:**

- Modify `src/notifications/repository.py:1-9` (add `import json`), `src/notifications/repository.py:18-84` (replace `create`; delete the now-obsolete `find_recent`).
- Modify `tests/test_notifications.py` (add `import json` to the imports at the top; append the three test functions below). **Also delete the now-obsolete legacy dispatcher coverage** in this file — the file-local `dispatcher` fixture and the three tests `test_notify_stores_in_db`, `test_notify_deduplicates`, `test_notify_disabled_does_nothing` (≈ lines 16–48). They call the pre-rewrite dispatcher, which uses the removed `find_recent` and the removed `channel=` kwarg on `create`; leaving them makes `tests/test_notifications.py` red from here until Task 9. The dispatcher's own coverage is re-established in Task 9 (`tests/test_notifications_dispatcher.py`). Keep the file-local `notif_repo` fixture (≈ lines 11–13) — the repository tests use it.

**Interfaces:** Consumes: Task 3's `notifications` schema (`channels`, `priority`, `dedup_key`, unique `idx_notifications_dedup`). Produces exactly:

```python
async def create(self, *, type: str, title: str, body: str, priority: str,
                 channels: list[str], status: str = "unread",
                 reference_type: str | None = None, reference_id: str | None = None,
                 dedup_key: str | None = None, group_key: str | None = None) -> str | None
```

Returns the new id, or `None` when the insert was deduped (`cursor.rowcount == 0`). This is the single write path the dispatcher (`await self._repo.create(...)`) and Task 5/6 read paths depend on. (The dispatcher rewrite in the Phase-1 dispatch cluster drops the old `find_recent`-based SELECT-then-INSERT dedup — this task removes `find_recent`; its only caller is that dispatcher.)

- [ ] **Step 1: Write the failing test** — add `import json` to the top of `tests/test_notifications.py`, then append:

```python
@pytest.mark.asyncio
async def test_create_returns_id_and_persists(notif_repo):
    nid = await notif_repo.create(
        type="meeting_processed", title="Notes ready", body="Your meeting is processed",
        priority="normal", channels=["in_app", "macos"],
        reference_type="meeting", reference_id="m-1", dedup_key="k-1",
    )
    assert nid is not None
    cur = await notif_repo._db.conn.execute(
        "SELECT channels, channel, priority, status, reference_type "
        "FROM notifications WHERE id=?",
        (nid,),
    )
    row = await cur.fetchone()
    assert json.loads(row["channels"]) == ["in_app", "macos"]
    assert row["channel"] == "in_app"          # legacy NOT NULL column = channels[0]
    assert row["priority"] == "normal"
    assert row["status"] == "unread"
    assert row["reference_type"] == "meeting"


@pytest.mark.asyncio
async def test_create_dedup_on_conflict(notif_repo):
    first = await notif_repo.create(
        type="task_overdue", title="Overdue", body="x", priority="low",
        channels=["in_app"], reference_id="item-1", dedup_key="dup-1",
    )
    second = await notif_repo.create(
        type="task_overdue", title="Overdue again", body="y", priority="low",
        channels=["in_app"], reference_id="item-1", dedup_key="dup-1",
    )
    assert first is not None
    assert second is None                       # deduped -> rowcount 0
    cur = await notif_repo._db.conn.execute("SELECT COUNT(*) FROM notifications")
    assert (await cur.fetchone())[0] == 1        # no second row inserted


@pytest.mark.asyncio
async def test_create_null_dedup_keys_coexist(notif_repo):
    a = await notif_repo.create(type="digest", title="A", body="", priority="low",
                                channels=["in_app"], dedup_key=None)
    b = await notif_repo.create(type="digest", title="B", body="", priority="low",
                                channels=["in_app"], dedup_key=None)
    assert a is not None and b is not None and a != b
    cur = await notif_repo._db.conn.execute("SELECT COUNT(*) FROM notifications")
    assert (await cur.fetchone())[0] == 2        # NULL dedup_keys are distinct
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_notifications.py::test_create_returns_id_and_persists tests/test_notifications.py::test_create_dedup_on_conflict tests/test_notifications.py::test_create_null_dedup_keys_coexist -q
```

Expected failure: `TypeError: create() got an unexpected keyword argument 'priority'` (the current signature is `create(self, type, title, body, channel, ...)`).

- [ ] **Step 3: Write minimal implementation** — in `src/notifications/repository.py`, add `import json` to the imports (top of file), then replace the `create` method **and** the entire `find_recent` method (lines 18-84) with just this new `create`:

```python
    async def create(
        self,
        *,
        type: str,
        title: str,
        body: str,
        priority: str,
        channels: list[str],
        status: str = "unread",
        reference_type: str | None = None,
        reference_id: str | None = None,
        dedup_key: str | None = None,
        group_key: str | None = None,
    ) -> str | None:
        """Insert a single notification row (one per logical event).

        Channels are stored as a JSON array; the legacy NOT NULL ``channel``
        column keeps ``channels[0]`` (or ``in_app``) for back-compat. Dedup is
        atomic at the DB level via ``ON CONFLICT(dedup_key) DO NOTHING``.
        Returns the new id, or ``None`` when the insert was deduped.
        """
        notif_id = str(uuid.uuid4())
        now = time.time()
        sent_at = now if status != "failed" else None
        channel = channels[0] if channels else "in_app"
        cursor = await self._db.conn.execute(
            """
            INSERT INTO notifications
                (id, type, reference_type, reference_id, channel, channels,
                 title, body, priority, status, dedup_key, group_key,
                 sent_at, read_at, snoozed_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedup_key) DO NOTHING
            """,
            (
                notif_id,
                type,
                reference_type,
                reference_id,
                channel,
                json.dumps(channels),
                title,
                body,
                priority,
                status,
                dedup_key,
                group_key,
                sent_at,
                None,
                None,
                now,
            ),
        )
        await self._db.conn.commit()
        if cursor.rowcount == 0:
            logger.debug("Notification deduped (dedup_key=%s)", dedup_key)
            return None
        logger.debug("Created notification %s (type=%s)", notif_id, type)
        return notif_id
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_notifications.py::test_create_returns_id_and_persists tests/test_notifications.py::test_create_dedup_on_conflict tests/test_notifications.py::test_create_null_dedup_keys_coexist -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```
git add src/notifications/repository.py tests/test_notifications.py
git commit -m "feat(notifications): one-row-per-event create with DB-level dedup

Rewrite NotificationRepository.create to a keyword-only signature that
stores channels as a JSON array and dedups atomically via
ON CONFLICT(dedup_key) DO NOTHING, returning None when deduped. Remove
the obsolete find_recent SELECT-then-INSERT dedup."
```

---

### Task 5: `NotificationRepository` lifecycle methods (list/count/read/dismiss/snooze)

**Files:**

- Modify `src/notifications/repository.py:86-148` (replace `list_notifications`, `count_unread`, `dismiss`; add `mark_read`, `mark_all_read`, `dismiss_all`, `snooze`) **and delete the Phase-0 `dismiss_all` that Task 2 appended after `dismiss` at end of file** — otherwise two `dismiss_all` definitions coexist and the later (v24) one wins.
- Modify `tests/test_notifications.py` (add `import time`; append the test functions below).

**Interfaces:** Consumes: Task 4's `create` and the v25 columns (`status`, `read_at`, `snoozed_until`, `channels`, `group_key`). Produces:

```python
async def list_notifications(self, *, limit: int = 50, offset: int = 0,
                             type: str | None = None,
                             include_dismissed: bool = False) -> list[dict]
async def count_unread(self, now: float | None = None) -> int
async def mark_read(self, notif_id: str) -> None
async def mark_all_read(self, *, type: str | None = None) -> int
async def dismiss(self, notif_id: str) -> None
async def dismiss_all(self, *, type: str | None = None) -> int
async def snooze(self, notif_id: str, until: float) -> None
```

`dismiss_all` is the **v25 superset** of the Phase-0 purge task's `dismiss_all` (same name/return-of-rows-affected contract, now excluding already-dismissed rows so the returned count is meaningful and the call is idempotent); the API `clear-all` route and Phase-0 purge both bind to this. Each `list_notifications` dict shape (`id,type,priority,reference_type,reference_id,channels(list),group_key,title,body,status,read_at,snoozed_until,created_at,sent_at`) is what the notifications REST routes and the UI serialize.

- [ ] **Step 1: Write the failing test** — add `import time` to the top of `tests/test_notifications.py`, then append:

```python
@pytest.mark.asyncio
async def test_list_excludes_dismissed_by_default(notif_repo):
    keep = await notif_repo.create(type="insight", title="Keep", body="",
                                   priority="high", channels=["in_app"])
    drop = await notif_repo.create(type="insight", title="Drop", body="",
                                   priority="high", channels=["in_app"])
    await notif_repo.dismiss(drop)

    items = await notif_repo.list_notifications()
    ids = [i["id"] for i in items]
    assert keep in ids and drop not in ids
    assert items[0]["channels"] == ["in_app"]   # channels round-trips as a list

    with_dismissed = await notif_repo.list_notifications(include_dismissed=True)
    assert drop in [i["id"] for i in with_dismissed]


@pytest.mark.asyncio
async def test_list_type_filter(notif_repo):
    await notif_repo.create(type="meeting_processed", title="M", body="",
                            priority="normal", channels=["in_app"])
    await notif_repo.create(type="task_overdue", title="T", body="",
                            priority="low", channels=["in_app"])
    only = await notif_repo.list_notifications(type="task_overdue")
    assert len(only) == 1 and only[0]["type"] == "task_overdue"


@pytest.mark.asyncio
async def test_count_unread_ignores_read_and_snoozed(notif_repo):
    a = await notif_repo.create(type="insight", title="A", body="",
                                priority="high", channels=["in_app"])
    await notif_repo.create(type="insight", title="B", body="",
                            priority="high", channels=["in_app"])
    c = await notif_repo.create(type="insight", title="C", body="",
                                priority="high", channels=["in_app"])
    assert await notif_repo.count_unread() == 3
    await notif_repo.mark_read(a)
    await notif_repo.snooze(c, time.time() + 3600)   # snoozed into the future
    assert await notif_repo.count_unread() == 1        # only B counts


@pytest.mark.asyncio
async def test_mark_read_only_affects_unread(notif_repo):
    a = await notif_repo.create(type="insight", title="A", body="",
                                priority="high", channels=["in_app"])
    await notif_repo.mark_read(a)
    cur = await notif_repo._db.conn.execute(
        "SELECT status, read_at FROM notifications WHERE id=?", (a,))
    row = await cur.fetchone()
    assert row["status"] == "read" and row["read_at"] is not None


@pytest.mark.asyncio
async def test_mark_all_read_with_and_without_type(notif_repo):
    await notif_repo.create(type="meeting_processed", title="M", body="",
                            priority="normal", channels=["in_app"])
    await notif_repo.create(type="task_overdue", title="T1", body="",
                            priority="low", channels=["in_app"])
    await notif_repo.create(type="task_overdue", title="T2", body="",
                            priority="low", channels=["in_app"])
    assert await notif_repo.mark_all_read(type="task_overdue") == 2
    assert await notif_repo.count_unread() == 1      # meeting_processed still unread
    assert await notif_repo.mark_all_read() == 1


@pytest.mark.asyncio
async def test_dismiss_all_is_idempotent(notif_repo):
    await notif_repo.create(type="insight", title="A", body="",
                            priority="high", channels=["in_app"])
    await notif_repo.create(type="insight", title="B", body="",
                            priority="high", channels=["in_app"])
    assert await notif_repo.dismiss_all() == 2
    assert await notif_repo.list_notifications() == []
    assert await notif_repo.dismiss_all() == 0       # nothing left to dismiss


@pytest.mark.asyncio
async def test_snooze_resets_to_unread(notif_repo):
    a = await notif_repo.create(type="insight", title="A", body="",
                                priority="high", channels=["in_app"])
    await notif_repo.mark_read(a)
    future = time.time() + 3600
    await notif_repo.snooze(a, future)
    cur = await notif_repo._db.conn.execute(
        "SELECT status, read_at, snoozed_until FROM notifications WHERE id=?", (a,))
    row = await cur.fetchone()
    assert row["status"] == "unread"
    assert row["read_at"] is None
    assert abs(row["snoozed_until"] - future) < 1
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_notifications.py -k "list_excludes_dismissed or type_filter or count_unread_ignores or mark_read or mark_all_read or dismiss_all_is_idempotent or snooze_resets" -q
```

Expected failure: `TypeError: list_notifications() got an unexpected keyword argument 'include_dismissed'` / `AttributeError: 'NotificationRepository' object has no attribute 'mark_read'` (the new-shaped methods don't exist yet).

- [ ] **Step 3: Write minimal implementation** — in `src/notifications/repository.py`, replace the existing `list_notifications`, `count_unread`, and `dismiss` methods (lines 86-148) with the following seven methods. **Also delete the Phase-0 `dismiss_all` method that Task 2 appended after `dismiss` (near the end of the file)** — the v25 `dismiss_all` below supersedes it. If both remain, Python keeps the later (v24 `WHERE status='sent'`) definition, which — after the migration backfills `sent`→`unread` — matches nothing, so `clear-all` and the idempotent purge silently no-op (and `test_dismiss_all_is_idempotent` fails).

```python
    async def list_notifications(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        type: str | None = None,
        include_dismissed: bool = False,
    ) -> list[dict]:
        """List notifications newest-first. Hides dismissed unless asked."""
        clauses: list[str] = []
        params: list = []
        if not include_dismissed:
            clauses.append("status != 'dismissed'")
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        cursor = await self._db.conn.execute(
            f"""
            SELECT id, type, priority, reference_type, reference_id, channels,
                   group_key, title, body, status, read_at, snoozed_until,
                   created_at, sent_at
            FROM notifications
            {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "priority": r["priority"],
                "reference_type": r["reference_type"],
                "reference_id": r["reference_id"],
                "channels": json.loads(r["channels"]) if r["channels"] else [],
                "group_key": r["group_key"],
                "title": r["title"],
                "body": r["body"],
                "status": r["status"],
                "read_at": r["read_at"],
                "snoozed_until": r["snoozed_until"],
                "created_at": r["created_at"],
                "sent_at": r["sent_at"],
            }
            for r in rows
        ]

    async def count_unread(self, now: float | None = None) -> int:
        """Count unread notifications that are not currently snoozed."""
        if now is None:
            now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE status = 'unread' AND (snoozed_until IS NULL OR snoozed_until <= ?)",
            (now,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def mark_read(self, notif_id: str) -> None:
        """Flip a single unread notification to read (no-op if already read)."""
        await self._db.conn.execute(
            "UPDATE notifications SET status='read', read_at=? "
            "WHERE id=? AND status='unread'",
            (time.time(), notif_id),
        )
        await self._db.conn.commit()

    async def mark_all_read(self, *, type: str | None = None) -> int:
        """Mark all unread (optionally of one type) read; return rows affected."""
        now = time.time()
        if type is not None:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status='read', read_at=? "
                "WHERE status='unread' AND type=?",
                (now, type),
            )
        else:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status='read', read_at=? WHERE status='unread'",
                (now,),
            )
        await self._db.conn.commit()
        return cursor.rowcount

    async def dismiss(self, notif_id: str) -> None:
        """Dismiss a single notification (removes it from the default view)."""
        await self._db.conn.execute(
            "UPDATE notifications SET status='dismissed' WHERE id=?",
            (notif_id,),
        )
        await self._db.conn.commit()

    async def dismiss_all(self, *, type: str | None = None) -> int:
        """Dismiss all non-dismissed (optionally of one type); return rows affected.

        Also serves the Phase-0 one-time purge (call with no type).
        """
        if type is not None:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status='dismissed' "
                "WHERE status != 'dismissed' AND type=?",
                (type,),
            )
        else:
            cursor = await self._db.conn.execute(
                "UPDATE notifications SET status='dismissed' WHERE status != 'dismissed'"
            )
        await self._db.conn.commit()
        return cursor.rowcount

    async def snooze(self, notif_id: str, until: float) -> None:
        """Snooze until ``until``; the row returns to unread and clears read_at."""
        await self._db.conn.execute(
            "UPDATE notifications SET snoozed_until=?, status='unread', read_at=NULL "
            "WHERE id=?",
            (until, notif_id),
        )
        await self._db.conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_notifications.py -k "list_excludes_dismissed or type_filter or count_unread_ignores or mark_read or mark_all_read or dismiss_all_is_idempotent or snooze_resets" -q
```

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```
git add src/notifications/repository.py tests/test_notifications.py
git commit -m "feat(notifications): repository lifecycle methods

Add read/dismiss/snooze lifecycle: list_notifications (type filter +
hide-dismissed, channels as JSON list), count_unread (unread and not
snoozed), mark_read, mark_all_read, dismiss, dismiss_all (idempotent
purge), snooze."
```

---

### Task 6: `NotificationRepository` rate-limit + retention (`count_recent`, `count_channel_since`, `prune`)

**Files:**

- Modify `src/notifications/repository.py` (append the three methods below to the class).
- Modify `tests/test_notifications.py` (append the module-level `_insert_notif` helper and test functions below).

**Interfaces:** Consumes: Task 3's `notifications` columns (`created_at`, `status`, `channels`). Produces:

```python
async def count_recent(self, type: str, since: float) -> int
async def count_channel_since(self, channel: str, since: float) -> int
async def prune(self, *, now: float, retention_days: int,
                dismissed_retention_days: int, max_rows: int) -> int
```

`count_recent` backs the dispatcher's per-type/global `max_per_hour` gate; `count_channel_since` backs the `email.max_per_day` cap (matches channel membership inside the JSON array, excludes `status='failed'`); `prune` (three delete passes → total deleted) backs the scheduled `_prune_notifications` server task.

- [ ] **Step 1: Write the failing test** — append to `tests/test_notifications.py`:

```python
async def _insert_notif(repo, *, nid, ntype="task_overdue", status="unread",
                        channels='["in_app"]', created_at=0.0):
    """Insert a row with a controlled created_at/status/channels (NULL dedup_key)."""
    await repo._db.conn.execute(
        "INSERT INTO notifications "
        "(id, type, channel, channels, title, body, priority, status, created_at) "
        "VALUES (?, ?, 'in_app', ?, 't', 'b', 'normal', ?, ?)",
        (nid, ntype, channels, status, created_at),
    )
    await repo._db.conn.commit()


@pytest.mark.asyncio
async def test_count_recent(notif_repo):
    now = 5_000_000_000.0
    await _insert_notif(notif_repo, nid="r1", ntype="task_overdue", created_at=now - 100)
    await _insert_notif(notif_repo, nid="r2", ntype="task_overdue", created_at=now - 5000)
    await _insert_notif(notif_repo, nid="r3", ntype="insight", created_at=now - 50)
    # Last hour, type task_overdue -> only r1 (r2 too old, r3 wrong type).
    assert await notif_repo.count_recent("task_overdue", now - 3600) == 1


@pytest.mark.asyncio
async def test_count_channel_since(notif_repo):
    now = 5_000_000_000.0
    await _insert_notif(notif_repo, nid="e1", channels='["email","in_app"]', created_at=now - 10)
    await _insert_notif(notif_repo, nid="e2", channels='["in_app"]', created_at=now - 10)
    await _insert_notif(notif_repo, nid="e3", channels='["email"]',
                        status="failed", created_at=now - 10)
    # e1 counts; e2 has no email; e3 failed doesn't count.
    assert await notif_repo.count_channel_since("email", now - 3600) == 1
    # Substring must not false-match ("app" vs "in_app").
    assert await notif_repo.count_channel_since("app", now - 3600) == 0


@pytest.mark.asyncio
async def test_prune_dismissed_retention(notif_repo):
    now = 5_000_000_000.0
    await _insert_notif(notif_repo, nid="d_old", status="dismissed",
                        created_at=now - 10 * 86400)
    await _insert_notif(notif_repo, nid="d_new", status="dismissed",
                        created_at=now - 2 * 86400)
    deleted = await notif_repo.prune(now=now, retention_days=365,
                                     dismissed_retention_days=7, max_rows=500)
    assert deleted == 1
    cur = await notif_repo._db.conn.execute("SELECT id FROM notifications")
    assert {r[0] for r in await cur.fetchall()} == {"d_new"}


@pytest.mark.asyncio
async def test_prune_age_retention(notif_repo):
    now = 5_000_000_000.0
    await _insert_notif(notif_repo, nid="old", status="unread",
                        created_at=now - 40 * 86400)
    await _insert_notif(notif_repo, nid="fresh", status="unread",
                        created_at=now - 5 * 86400)
    deleted = await notif_repo.prune(now=now, retention_days=30,
                                     dismissed_retention_days=7, max_rows=500)
    assert deleted == 1
    cur = await notif_repo._db.conn.execute("SELECT id FROM notifications")
    assert {r[0] for r in await cur.fetchall()} == {"fresh"}


@pytest.mark.asyncio
async def test_prune_max_rows_cap(notif_repo):
    now = 5_000_000_000.0
    for i in range(5):  # m0 newest ... m4 oldest
        await _insert_notif(notif_repo, nid=f"m{i}", status="unread",
                            created_at=now - i * 100)
    # All recent, none dismissed -> only the hard cap deletes.
    deleted = await notif_repo.prune(now=now, retention_days=365,
                                     dismissed_retention_days=365, max_rows=2)
    assert deleted == 3
    cur = await notif_repo._db.conn.execute(
        "SELECT id FROM notifications ORDER BY created_at DESC")
    assert [r[0] for r in await cur.fetchall()] == ["m0", "m1"]  # two newest survive
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_notifications.py -k "count_recent or count_channel_since or prune_" -q
```

Expected failure: `AttributeError: 'NotificationRepository' object has no attribute 'count_recent'` (none of the three methods exist yet).

- [ ] **Step 3: Write minimal implementation** — append these three methods to the `NotificationRepository` class in `src/notifications/repository.py`:

```python
    async def count_recent(self, type: str, since: float) -> int:
        """Count notifications of ``type`` created at/after ``since`` (rate limit)."""
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE type=? AND created_at>=?",
            (type, since),
        )
        row = await cursor.fetchone()
        return row[0]

    async def count_channel_since(self, channel: str, since: float) -> int:
        """Count non-failed notifications delivered on ``channel`` since ``since``.

        ``channels`` is a JSON array string; membership is matched by the
        quoted channel token so substrings ('app' vs 'in_app') never collide.
        """
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE created_at >= ? AND status != 'failed' "
            "AND channels LIKE '%\"' || ? || '\"%'",
            (since, channel),
        )
        row = await cursor.fetchone()
        return row[0]

    async def prune(
        self,
        *,
        now: float,
        retention_days: int,
        dismissed_retention_days: int,
        max_rows: int,
    ) -> int:
        """Three-pass retention prune; return the total rows deleted.

        1. dismissed rows older than ``dismissed_retention_days``,
        2. any row older than ``retention_days``,
        3. the oldest rows beyond a hard ``max_rows`` cap.
        """
        total = 0
        cursor = await self._db.conn.execute(
            "DELETE FROM notifications WHERE status='dismissed' AND created_at < ?",
            (now - dismissed_retention_days * 86400,),
        )
        total += cursor.rowcount
        cursor = await self._db.conn.execute(
            "DELETE FROM notifications WHERE created_at < ?",
            (now - retention_days * 86400,),
        )
        total += cursor.rowcount
        cursor = await self._db.conn.execute(
            "DELETE FROM notifications WHERE id IN ("
            "  SELECT id FROM notifications ORDER BY created_at DESC LIMIT -1 OFFSET ?"
            ")",
            (max_rows,),
        )
        total += cursor.rowcount
        await self._db.conn.commit()
        return total
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_notifications.py -k "count_recent or count_channel_since or prune_" -q
```

Expected: `5 passed`.

- [ ] **Step 5: Commit**

```
git add src/notifications/repository.py tests/test_notifications.py
git commit -m "feat(notifications): repository rate-limit and retention

Add count_recent (per-type hourly cap), count_channel_since (per-channel
cap via quoted JSON-token match, excluding failed), and prune with three
delete passes (dismissed-age, global-age, hard max_rows cap)."
```

---

### Task 7: NotificationsConfig new fields + config.example.yaml

**Files:**

- Modify `src/utils/config.py:347-355` (the `NotificationsConfig` dataclass)
- Modify `config.example.yaml:427-429` (the notifications "Reminder timing" block)
- Test `tests/test_notifications_config.py` (new)

**Interfaces:** Consumes: `_build_dataclass` + `load_config` (`src/utils/config.py:425,474`), which already filter unknown YAML keys to declared fields. Produces: the `NotificationsConfig` fields consumed by Task 9's dispatcher (`macos_sound`, `muted_types`, `macos_min_priority`, `external_min_priority`, `max_per_hour`, `per_type_max_per_hour`, `quiet_hours_enabled`, `quiet_start`, `quiet_end`, `email.max_per_day`) and by Tasks 10+ (`task_digest`, `digest_time`, `overdue_recheck_minutes`, `retention_days`, `dismissed_retention_days`, `max_rows`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notifications_config.py
"""Tests for the extended NotificationsConfig schema (v25 redesign)."""

import textwrap
from pathlib import Path

from src.utils.config import NotificationsConfig, load_config


def test_notifications_config_defaults():
    c = NotificationsConfig()
    assert c.macos_sound is False
    assert c.muted_types == []
    assert c.macos_min_priority == "normal"
    assert c.external_min_priority == "high"
    assert c.max_per_hour == 12
    assert c.per_type_max_per_hour == {}
    assert c.quiet_hours_enabled is True
    assert c.quiet_start == "22:00"
    assert c.quiet_end == "08:00"
    assert c.task_digest == "daily"
    assert c.digest_time == "08:00"
    assert c.overdue_recheck_minutes == 360
    assert c.dedup_window_minutes == 60
    assert c.retention_days == 30
    assert c.dismissed_retention_days == 7
    assert c.max_rows == 500
    # legacy fields remain present (parsed, unused) so old config.yaml still loads
    assert c.default_reminder_before_due == "1d"
    assert c.overdue_check_interval == "6h"


def test_notifications_config_loads_new_keys(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            notifications:
              enabled: true
              macos_sound: true
              muted_types: ["task_overdue", "automation"]
              macos_min_priority: "high"
              external_min_priority: "normal"
              max_per_hour: 5
              per_type_max_per_hour:
                meeting_processed: 3
              quiet_hours_enabled: false
              quiet_start: "23:30"
              quiet_end: "07:15"
              task_digest: "off"
              digest_time: "09:00"
              overdue_recheck_minutes: 120
              dedup_window_minutes: 30
              retention_days: 14
              dismissed_retention_days: 3
              max_rows: 250
            """
        )
    )
    n = load_config(cfg).notifications
    assert n.macos_sound is True
    assert n.muted_types == ["task_overdue", "automation"]
    assert n.macos_min_priority == "high"
    assert n.external_min_priority == "normal"
    assert n.max_per_hour == 5
    assert n.per_type_max_per_hour == {"meeting_processed": 3}
    assert n.quiet_hours_enabled is False
    assert n.quiet_start == "23:30"
    assert n.quiet_end == "07:15"
    assert n.task_digest == "off"
    assert n.digest_time == "09:00"
    assert n.overdue_recheck_minutes == 120
    assert n.dedup_window_minutes == 30
    assert n.retention_days == 14
    assert n.dismissed_retention_days == 3
    assert n.max_rows == 250


def test_notifications_config_legacy_keys_still_parse(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            notifications:
              enabled: true
              in_app: true
              macos: true
              default_reminder_before_due: "2d"
              overdue_check_interval: "12h"
            """
        )
    )
    n = load_config(cfg).notifications
    # legacy keys parse without error and populate
    assert n.default_reminder_before_due == "2d"
    assert n.overdue_check_interval == "12h"
    # new fields fall back to defaults
    assert n.max_per_hour == 12
    assert n.macos_sound is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_notifications_config.py -q
```

Expected failure: `AttributeError: 'NotificationsConfig' object has no attribute 'macos_sound'` in `test_notifications_config_defaults`.

- [ ] **Step 3: Write minimal implementation**

Replace the `NotificationsConfig` dataclass at `src/utils/config.py:347-355` with:

```python
@dataclass
class NotificationsConfig:
    enabled: bool = True
    in_app: bool = True
    macos: bool = True
    macos_sound: bool = False
    webhook: WebhookChannelConfig = field(default_factory=WebhookChannelConfig)
    email: EmailChannelConfig = field(default_factory=EmailChannelConfig)
    # per-type mute (list of event types to silence entirely)
    muted_types: list = field(default_factory=list)
    # routing
    macos_min_priority: str = "normal"
    external_min_priority: str = "high"
    # rate limits
    max_per_hour: int = 12
    per_type_max_per_hour: dict = field(default_factory=dict)
    # quiet hours
    quiet_hours_enabled: bool = True
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    # digest
    task_digest: str = "daily"
    digest_time: str = "08:00"
    # cadence / dedup
    overdue_recheck_minutes: int = 360
    dedup_window_minutes: int = 60
    # retention
    retention_days: int = 30
    dismissed_retention_days: int = 7
    max_rows: int = 500
    # Legacy — parsed for backward-compat but no longer drive cadence.
    default_reminder_before_due: str = "1d"
    overdue_check_interval: str = "6h"
```

Then replace the notifications "Reminder timing" block at `config.example.yaml:427-429`:

```yaml
# --- Reminder timing (legacy; parsed but no longer drives cadence) ---
default_reminder_before_due: "1d"
overdue_check_interval: "6h"

# --- Behaviour ---
macos_sound: false # play a sound with macOS banners (off by default)
muted_types: [] # event types to silence entirely, e.g. ["task_overdue"]
macos_min_priority: "normal" # min priority that may raise a macOS banner
external_min_priority: "high" # min priority for webhook/email delivery
max_per_hour: 12 # global cap on notifications per type per hour
per_type_max_per_hour: {} # per-type overrides, e.g. {automation: 4}
quiet_hours_enabled: true # suppress banners/sound during quiet hours
quiet_start: "22:00" # local HH:MM
quiet_end: "08:00" # local HH:MM (window may wrap past midnight)
task_digest: "daily" # "daily" or "off"
digest_time: "08:00" # local HH:MM the daily digest fires
overdue_recheck_minutes: 360 # real cadence for the overdue sweep
dedup_window_minutes: 60 # collapse duplicate events within this window
retention_days: 30 # prune anything older than this
dismissed_retention_days: 7 # prune dismissed rows older than this
max_rows: 500 # hard cap; delete oldest rows beyond this
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_notifications_config.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_notifications_config.py
git commit -m "feat(config): extend NotificationsConfig with routing, quiet-hours, rate-limit and retention fields"
```

---

### Task 8: Channel contract coverage — lock macOS returncode/sound + email bool (no impl change)

> **Note:** This is a **coverage-only** task — there is no implementation change. Task 1 (Phase 0) already gave `macos.send` its `*, sound=False -> bool` contract (real `osascript` returncode + opt-in sound), and `external.send_email` is unchanged. These tests lock the channel contracts that Task 9's `_send_channel` depends on; they pass immediately against the existing code (no red phase).

**Files:**

- **No implementation change** — `src/notifications/channels/macos.py` already has the contract (Task 1); `src/notifications/channels/external.py` is unchanged.
- Test `tests/test_notification_channels.py` (new)

**Interfaces:** Consumes: `macos.send(title, body, *, sound: bool = False, subtitle: str = "") -> bool` as implemented in **Task 1** (returns `True` only when `osascript` returncode == 0; adds the `sound name "default"` line only when `sound=True`), and `NotificationsConfig.macos_sound` (Task 7). Produces: no new code — regression coverage locking the macOS returncode/sound contract and `external.send_email(config, title, body) -> bool` (no per-day cap; the cap lives in the dispatcher via `count_channel_since`), both consumed by Task 9's `_send_channel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notification_channels.py
"""Channel-level correctness tests: macOS returncode/sound + email bool contract."""

import asyncio

import pytest

from src.notifications.channels import external, macos
from src.utils.config import EmailChannelConfig


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self):
        return (b"", b"" if self.returncode == 0 else b"boom")


@pytest.mark.asyncio
async def test_macos_send_returns_true_on_zero_returncode(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok = await macos.send("Title", "Body", sound=False)
    assert ok is True
    # sound off => no sound clause in the AppleScript
    script = captured["args"][2]
    assert "sound name" not in script


@pytest.mark.asyncio
async def test_macos_send_returns_false_on_nonzero_returncode(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok = await macos.send("Title", "Body", sound=True)
    assert ok is False


@pytest.mark.asyncio
async def test_macos_send_sound_flag_adds_sound_clause(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok = await macos.send("Title", "Body", sound=True)
    assert ok is True
    script = captured["args"][2]
    assert 'sound name "default"' in script


@pytest.mark.asyncio
async def test_send_email_returns_false_when_unconfigured():
    # send_email enforces NO per-day cap itself; it only returns a bool.
    result = await external.send_email(EmailChannelConfig(), "subject", "body")
    assert result is False
```

- [ ] **Step 2: Run the tests — they pass against the existing implementation**

```bash
python -m pytest tests/test_notification_channels.py -q
```

Expected: `4 passed`. There is **no red phase**: `macos.send` already has the `sound`/returncode contract from Task 1, and `send_email` already returns a bool for an unconfigured channel. This task exists to _lock_ those contracts (Task 9 relies on them); it adds no implementation.

(For reference, the `macos.send` implementation these tests lock — already in place from Task 1 — is:)

```python
async def send(title: str, body: str, *, sound: bool = False, subtitle: str = "") -> bool:
    """Show a macOS notification via osascript. Returns True only on returncode 0."""
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_subtitle = subtitle.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    if safe_subtitle:
        script += f' subtitle "{safe_subtitle}"'
    if sound:
        script += ' sound name "default"'
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("osascript failed: %s", stderr.decode())
            return False
        return True
    except Exception as e:
        logger.warning("macOS notification failed: %s", e)
        return False
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_notification_channels.py
git commit -m "test(notifications): lock macOS returncode/sound and email bool channel contracts"
```

---

### Task 9: NotificationDispatcher rewrite — full gate pipeline

**Files:**

- Rewrite `src/notifications/dispatcher.py` (whole file, currently lines 1-115)
- (The obsolete `dispatcher` fixture + 3 legacy dispatcher tests in `tests/test_notifications.py` were **already removed in Task 4** when `create`/`find_recent` changed — nothing to remove here.)
- Test `tests/test_notifications_dispatcher.py` (new) — defines its own file-local `notif_repo`/dispatcher fixtures.

**Interfaces:**
Consumes (from earlier Phase-1 tasks — must exist before this task runs):

- `NotificationRepository.create(*, type, title, body, priority, channels: list[str], status="unread", reference_type=None, reference_id=None, dedup_key=None, group_key=None) -> str | None` (returns `None` when deduped via `ON CONFLICT(dedup_key)`)
- `NotificationRepository.count_recent(type, since) -> int`
- `NotificationRepository.count_channel_since(channel, since) -> int`
- `NotificationRepository.count_unread(now=None) -> int` and `list_notifications(*, limit=50, ...) -> list[dict]` (each dict carries `channels` as a `list`)
- schema v25 migration (adds `dedup_key`/`channels`/`priority`/... columns + the unique dedup index)
- `macos.send(title, body, *, sound, subtitle) -> bool` (Task 1; contract-locked in Task 8), `external.send_webhook/send_email -> bool`, `in_app.send(event_bus, title, body, type, reference_id)`
- `NotificationsConfig` fields (Task 7)

Produces: `async def NotificationDispatcher.notify(*, type, title, body, priority="normal", reference_type=None, reference_id=None, dedup_key=None, group_key=None, now=None) -> str | None` — the single chokepoint that Tasks 10+ (producers: `_check_reminders`, digest, automations) call; plus helpers `_resolve_channels`, `_priority_ge`, `_in_quiet_hours`, and module-level `_start_of_day`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notifications_dispatcher.py
"""Gate-by-gate tests for the rewritten NotificationDispatcher (one test per gate)."""

import time

import pytest

from src.notifications.channels import external, macos
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.repository import NotificationRepository
from src.utils.config import (
    EmailChannelConfig,
    NotificationsConfig,
    WebhookChannelConfig,
)


def _local_epoch_at(hour: int, minute: int = 0) -> float:
    """Epoch for today at the given LOCAL wall-clock hour:minute (tz-independent)."""
    lt = list(time.localtime())
    lt[3] = hour
    lt[4] = minute
    lt[5] = 0
    return time.mktime(time.struct_time(tuple(lt)))


@pytest.fixture
async def notif_repo(db):
    return NotificationRepository(db)


def _make(config, notif_repo, event_bus):
    return NotificationDispatcher(config=config, repo=notif_repo, event_bus=event_bus)


@pytest.fixture(autouse=True)
def stub_channels(monkeypatch):
    """Record macOS/webhook/email sends instead of hitting osascript/SMTP/HTTP."""
    calls = {"macos": [], "webhook": [], "email": []}

    async def fake_macos(title, body, *, sound=False, subtitle=""):
        calls["macos"].append((title, body, sound))
        return True

    async def fake_webhook(config, title, body, type):
        calls["webhook"].append((title, body))
        return True

    async def fake_email(config, title, body):
        calls["email"].append((title, body))
        return True

    monkeypatch.setattr(macos, "send", fake_macos)
    monkeypatch.setattr(external, "send_webhook", fake_webhook)
    monkeypatch.setattr(external, "send_email", fake_email)
    return calls


# --- Gate 1: master off / muted ------------------------------------------------
@pytest.mark.asyncio
async def test_master_off_returns_none(notif_repo, event_bus):
    d = _make(NotificationsConfig(enabled=False), notif_repo, event_bus)
    assert await d.notify(type="meeting_processed", title="x", body="y", reference_id="m1") is None
    assert await notif_repo.count_unread() == 0


@pytest.mark.asyncio
async def test_muted_type_returns_none(notif_repo, event_bus):
    d = _make(
        NotificationsConfig(enabled=True, muted_types=["automation"]), notif_repo, event_bus
    )
    assert await d.notify(type="automation", title="x", body="y", reference_id="m1") is None
    assert await notif_repo.count_unread() == 0


# --- Gate 2: DB-level dedup ----------------------------------------------------
@pytest.mark.asyncio
async def test_dedup_returns_none_on_second(notif_repo, event_bus):
    d = _make(
        NotificationsConfig(enabled=True, in_app=True, macos=False), notif_repo, event_bus
    )
    first = await d.notify(type="meeting_processed", title="Done", body="ready", reference_id="m1")
    second = await d.notify(type="meeting_processed", title="Done", body="ready", reference_id="m1")
    assert first is not None
    assert second is None
    assert len(await notif_repo.list_notifications(limit=10)) == 1


# --- Gate 3: rate limit --------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limit_returns_none_past_cap(notif_repo, event_bus):
    config = NotificationsConfig(
        enabled=True,
        in_app=True,
        macos=False,
        per_type_max_per_hour={"meeting_processed": 1},
    )
    d = _make(config, notif_repo, event_bus)
    first = await d.notify(type="meeting_processed", title="a", body="b", reference_id="m1")
    second = await d.notify(type="meeting_processed", title="c", body="d", reference_id="m2")
    assert first is not None
    assert second is None
    assert len(await notif_repo.list_notifications(limit=10)) == 1


# --- Gate 4/5: priority + quiet-hours routing ----------------------------------
@pytest.mark.asyncio
async def test_low_priority_does_not_resolve_macos(notif_repo, event_bus, stub_channels):
    config = NotificationsConfig(
        enabled=True,
        in_app=True,
        macos=True,
        macos_min_priority="normal",
        quiet_hours_enabled=False,
    )
    d = _make(config, notif_repo, event_bus)
    await d.notify(
        type="task_overdue", title="Overdue", body="do it", priority="low", reference_id="a1"
    )
    assert stub_channels["macos"] == []
    items = await notif_repo.list_notifications(limit=10)
    assert items[0]["channels"] == ["in_app"]


@pytest.mark.asyncio
async def test_quiet_hours_suppresses_macos(notif_repo, event_bus, stub_channels):
    config = NotificationsConfig(
        enabled=True,
        in_app=True,
        macos=True,
        macos_min_priority="normal",
        quiet_hours_enabled=True,
        quiet_start="22:00",
        quiet_end="08:00",
    )
    d = _make(config, notif_repo, event_bus)
    # 23:00 local is inside a 22:00->08:00 (wrapping) quiet window.
    await d.notify(
        type="meeting_processed",
        title="Done",
        body="ready",
        priority="normal",
        reference_id="m1",
        now=_local_epoch_at(23, 0),
    )
    assert stub_channels["macos"] == []
    # ...but at noon (outside quiet hours) the banner resolves.
    await d.notify(
        type="meeting_processed",
        title="Done2",
        body="ready2",
        priority="normal",
        reference_id="m2",
        now=_local_epoch_at(12, 0),
    )
    assert len(stub_channels["macos"]) == 1


@pytest.mark.asyncio
async def test_high_priority_resolves_webhook_and_email(notif_repo, event_bus, stub_channels):
    config = NotificationsConfig(
        enabled=True,
        in_app=True,
        macos=False,
        webhook=WebhookChannelConfig(enabled=True, url="https://example.com/hook"),
        email=EmailChannelConfig(
            enabled=True, smtp_host="smtp.test", to_address="x@y.z", max_per_day=5
        ),
        external_min_priority="high",
        quiet_hours_enabled=False,
    )
    d = _make(config, notif_repo, event_bus)
    await d.notify(type="insight", title="Risk", body="flagged", priority="high", reference_id="i1")
    assert len(stub_channels["webhook"]) == 1
    assert len(stub_channels["email"]) == 1
    items = await notif_repo.list_notifications(limit=10)
    assert set(items[0]["channels"]) == {"in_app", "webhook", "email"}


# --- Gate 6: email per-day cap -------------------------------------------------
@pytest.mark.asyncio
async def test_email_cap_drops_email_past_max_per_day(notif_repo, event_bus, stub_channels):
    config = NotificationsConfig(
        enabled=True,
        in_app=True,
        macos=False,
        webhook=WebhookChannelConfig(enabled=False),
        email=EmailChannelConfig(
            enabled=True, smtp_host="smtp.test", to_address="x@y.z", max_per_day=1
        ),
        external_min_priority="high",
        quiet_hours_enabled=False,
    )
    d = _make(config, notif_repo, event_bus)
    first = await d.notify(type="insight", title="A", body="a", priority="high", reference_id="i1")
    second = await d.notify(type="insight", title="B", body="b", priority="high", reference_id="i2")
    assert first is not None
    assert second is not None
    assert len(stub_channels["email"]) == 1
    items = await notif_repo.list_notifications(limit=10)  # newest first
    assert "email" not in items[0]["channels"]
    assert "email" in items[1]["channels"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_notifications_dispatcher.py -q
```

Expected failure: `TypeError: notify() got an unexpected keyword argument 'reference_type'` (the current `notify` is positional `type,title,body,reference_id,...` with no gate pipeline). Several tests also fail on `KeyError: 'channels'` because the current dispatcher writes one row per channel and `list_notifications` returns no `channels` list.

- [ ] **Step 3: Write minimal implementation**

Replace the entire contents of `src/notifications/dispatcher.py` with:

```python
"""Notification dispatcher — the single governed chokepoint for all producers."""

import asyncio
import logging
import time

from src.api.events import EventBus
from src.notifications.channels import external, in_app, macos
from src.notifications.repository import NotificationRepository
from src.utils.config import NotificationsConfig

logger = logging.getLogger("contextrecall.notifications.dispatcher")

_PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2}


def _start_of_day(now: float) -> float:
    """Return the local-midnight epoch for the day containing ``now``."""
    lt = time.localtime(now)
    midnight = time.struct_time(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst)
    )
    return time.mktime(midnight)


class NotificationDispatcher:
    """Fan-out delivery behind a fixed gate pipeline: mute -> dedup -> rate limit
    -> priority/quiet-hours routing -> email cap -> gather delivery -> single persist."""

    def __init__(
        self,
        config: NotificationsConfig,
        repo: NotificationRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._repo = repo
        self._event_bus = event_bus

    async def notify(
        self,
        *,
        type: str,
        title: str,
        body: str,
        priority: str = "normal",
        reference_type: str | None = None,
        reference_id: str | None = None,
        dedup_key: str | None = None,
        group_key: str | None = None,
        now: float | None = None,
    ) -> str | None:
        # 1. clock
        if now is None:
            now = time.time()

        # 2. master + per-type mute
        if not self._config.enabled or type in self._config.muted_types:
            return None

        # 3. dedup key (stable per type+reference+day-bucket)
        if dedup_key is None:
            dedup_key = f"{type}:{reference_id}:{int(now // 86400)}"

        # 4. rate limit (per-type override, else global)
        cap = self._config.per_type_max_per_hour.get(type, self._config.max_per_hour)
        if await self._repo.count_recent(type, now - 3600) >= cap:
            logger.info("Rate limit hit for type=%s (cap=%d); dropping notification", type, cap)
            return None

        # 5. priority -> channel routing (quiet hours applied to macOS)
        channels = self._resolve_channels(priority, now)

        # 6. email per-day cap
        if "email" in channels and (
            await self._repo.count_channel_since("email", _start_of_day(now))
            >= self._config.email.max_per_day
        ):
            channels.remove("email")

        # 7. in-app fallback
        if not channels:
            channels = ["in_app"] if self._config.in_app else []
            if not channels:
                return None

        # 8. deliver concurrently, collect successes
        results = await asyncio.gather(
            *(self._send_channel(c, type, title, body, reference_id) for c in channels),
            return_exceptions=True,
        )
        delivered = [c for c, ok in zip(channels, results) if ok is True]

        # 9. single persistent row
        status = "unread" if delivered else "failed"
        return await self._repo.create(
            type=type,
            title=title,
            body=body,
            priority=priority,
            channels=delivered or channels,
            status=status,
            reference_type=reference_type,
            reference_id=reference_id,
            dedup_key=dedup_key,
            group_key=group_key,
        )

    # --- helpers ---------------------------------------------------------------
    def _resolve_channels(self, priority: str, now: float) -> list[str]:
        channels: list[str] = []
        if self._config.in_app:
            channels.append("in_app")
        if (
            self._config.macos
            and self._priority_ge(priority, self._config.macos_min_priority)
            and not self._in_quiet_hours(now)
        ):
            channels.append("macos")
        if self._config.webhook.enabled and self._priority_ge(
            priority, self._config.external_min_priority
        ):
            channels.append("webhook")
        if self._config.email.enabled and self._priority_ge(
            priority, self._config.external_min_priority
        ):
            channels.append("email")
        return channels

    @staticmethod
    def _priority_ge(a: str, b: str) -> bool:
        return _PRIORITY_ORDER.get(a, 1) >= _PRIORITY_ORDER.get(b, 1)

    def _in_quiet_hours(self, now: float) -> bool:
        if not self._config.quiet_hours_enabled:
            return False
        start = self._parse_hhmm(self._config.quiet_start)
        end = self._parse_hhmm(self._config.quiet_end)
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min
        if start <= end:
            return start <= cur < end
        # window wraps past midnight (e.g. 22:00 -> 08:00)
        return cur >= start or cur < end

    @staticmethod
    def _parse_hhmm(value: str) -> int:
        try:
            hh, mm = value.split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return 0

    async def _send_channel(
        self,
        channel: str,
        type: str,
        title: str,
        body: str,
        reference_id: str | None,
    ) -> bool:
        """Dispatch to a single channel. Returns True only on real success."""
        try:
            if channel == "in_app":
                if self._event_bus is not None:
                    await in_app.send(self._event_bus, title, body, type, reference_id)
                    return True
                logger.warning("in_app channel requested but no EventBus available")
                return False
            if channel == "macos":
                return await macos.send(title, body, sound=self._config.macos_sound)
            if channel == "webhook":
                return await external.send_webhook(self._config.webhook, title, body, type)
            if channel == "email":
                return await external.send_email(self._config.email, title, body)
            logger.warning("Unknown notification channel: %s", channel)
            return False
        except Exception:
            logger.exception("Failed to send via channel %s", channel)
            return False
```

Then remove the obsolete dispatcher fixture and legacy tests from `tests/test_notifications.py` (delete lines 16-48: the `dispatcher` fixture plus `test_notify_stores_in_db`, `test_notify_deduplicates`, `test_notify_disabled_does_nothing`, which used the pre-rewrite `notify` signature and `status='sent'`). Leave the `notif_repo` fixture and the imports intact for the repository tests owned by earlier tasks.

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_notifications_dispatcher.py tests/test_notifications.py -q
```

Expected: `8 passed` in `tests/test_notifications_dispatcher.py`, and `tests/test_notifications.py` green (the 3 legacy dispatcher tests removed; repository tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/notifications/dispatcher.py tests/test_notifications_dispatcher.py tests/test_notifications.py
git commit -m "feat(notifications): rewrite dispatcher with governed gate pipeline (mute/dedup/rate-limit/routing/quiet-hours/email-cap)"
```

---

### Task 10: `_check_reminders` rewrite — transition-based overdue + reminder clearing

**Files:**

- Modify `src/action_items/repository.py:238` (`list_overdue` add `limit`), `:250` (`list_due_reminders` add `limit`), and append two new methods `mark_overdue_notified` / `clear_reminder` after `:260`.
- Modify `src/api/server.py:504` (`_check_reminders` full rewrite).
- Test (create) `tests/test_reminders_producer.py`.

**Interfaces:**

- Consumes: schema v25 `action_items.overdue_notified_at REAL` (migration task); `NotificationDispatcher.notify(*, type, title, body, priority="normal", reference_type=None, reference_id=None, dedup_key=None, group_key=None, now=None) -> str | None` (dispatcher task); `NotificationsConfig` new fields (config task); `NotificationRepository.create/list_notifications` (storage task); `ActionItemRepository.create/get/list_overdue/list_due_reminders`.
- Produces: `ActionItemRepository.mark_overdue_notified(self, item_id: str, now: float) -> None`; `ActionItemRepository.clear_reminder(self, item_id: str) -> None`; `list_overdue(self, limit: int = 100)`, `list_due_reminders(self, limit: int = 100)`; a `_check_reminders` that emits `task_overdue`/`task_reminder` at priority `low`, once each. Task 13 later swaps the inline dispatcher build for `self._get_notification_dispatcher()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reminders_producer.py
"""Producer tests: overdue notifies once per transition; reminders clear."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.action_items.repository import ActionItemRepository
from src.api.server import ApiServer
from src.notifications.dispatcher import NotificationDispatcher
from src.utils.config import NotificationsConfig


def _cfg():
    # in_app on, macos off → no osascript ever runs in the suite.
    return SimpleNamespace(
        notifications=NotificationsConfig(enabled=True, in_app=True, macos=False)
    )


@pytest.mark.asyncio
async def test_overdue_notifies_once_across_two_sweeps(db, event_bus):
    server = ApiServer()
    server.db = db
    server.event_bus = event_bus
    ai_repo = ActionItemRepository(db)
    item_id = await ai_repo.create(
        meeting_id="m1", title="Ship release", due_date="2020-01-01", status="open"
    )

    with patch.object(NotificationDispatcher, "notify", new=AsyncMock(return_value="n1")) as notify:
        with patch("src.api.server.load_config", return_value=_cfg()):
            await server._check_reminders()
            await server._check_reminders()

    overdue_calls = [c for c in notify.await_args_list if c.kwargs.get("type") == "task_overdue"]
    assert len(overdue_calls) == 1  # once, not once-per-sweep
    assert overdue_calls[0].kwargs["priority"] == "low"
    assert overdue_calls[0].kwargs["reference_type"] == "action_item"
    assert overdue_calls[0].kwargs["reference_id"] == item_id
    row = await ai_repo.get(item_id)
    assert row["overdue_notified_at"] is not None


@pytest.mark.asyncio
async def test_reminder_fires_and_clears_reminder_at(db, event_bus):
    server = ApiServer()
    server.db = db
    server.event_bus = event_bus
    ai_repo = ActionItemRepository(db)
    item_id = await ai_repo.create(
        meeting_id="m1", title="Call client", reminder_at=time.time() - 60, status="open"
    )

    with patch.object(NotificationDispatcher, "notify", new=AsyncMock(return_value="n1")) as notify:
        with patch("src.api.server.load_config", return_value=_cfg()):
            await server._check_reminders()

    reminder_calls = [c for c in notify.await_args_list if c.kwargs.get("type") == "task_reminder"]
    assert len(reminder_calls) == 1
    assert reminder_calls[0].kwargs["priority"] == "low"
    row = await ai_repo.get(item_id)
    assert row["reminder_at"] is None  # cleared so the next sweep won't re-select it
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_reminders_producer.py -q
```

Expected: FAIL. `test_overdue_notifies_once_across_two_sweeps` fails `assert len(overdue_calls) == 1` (the current `_check_reminders` emits `type="overdue"`, not `"task_overdue"`, so the filtered list is empty), and `test_reminder_fires_and_clears_reminder_at` fails `assert row["reminder_at"] is None` (nothing clears it today).

- [ ] **Step 3: Write minimal implementation**

In `src/action_items/repository.py`, replace `list_overdue` (`:238`) and `list_due_reminders` (`:250`) and append the two new methods:

```python
    async def list_overdue(self, limit: int = 100) -> list[dict]:
        """List action items that are overdue (open/in_progress with due_date in the past)."""
        today = date.today().isoformat()
        cursor = await self._db.conn.execute(
            "SELECT * FROM action_items "
            "WHERE status IN ('open', 'in_progress') AND due_date < ? "
            "ORDER BY due_date ASC LIMIT ?",
            (today, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_due_reminders(self, limit: int = 100) -> list[dict]:
        """List action items with reminders that are due now or in the past."""
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM action_items "
            "WHERE status IN ('open', 'in_progress') AND reminder_at <= ? "
            "ORDER BY reminder_at ASC LIMIT ?",
            (now, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_overdue_notified(self, item_id: str, now: float) -> None:
        """Record that the one-time overdue notification for *item_id* has fired."""
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE action_items SET overdue_notified_at = ? WHERE id = ?",
                (now, item_id),
            )
            await self._db.conn.commit()

    async def clear_reminder(self, item_id: str) -> None:
        """Clear a fired reminder so ``list_due_reminders`` stops re-selecting it."""
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE action_items SET reminder_at = NULL WHERE id = ?",
                (item_id,),
            )
            await self._db.conn.commit()
```

In `src/api/server.py`, replace the whole `_check_reminders` method (`:504`–`:533`) with:

```python
    async def _check_reminders(self) -> None:
        """Notify once when action items become overdue, and fire due reminders.

        Overdue items notify a single time on the null→now transition of
        ``overdue_notified_at`` (their ongoing state is carried by the daily
        digest). Reminders fire once, then ``reminder_at`` is cleared so the
        next sweep does not re-select them.
        """
        import time

        from src.action_items.repository import ActionItemRepository
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository

        config = load_config()
        now = time.time()
        ai_repo = ActionItemRepository(self.db)
        dispatcher = NotificationDispatcher(
            config=config.notifications,
            repo=NotificationRepository(self.db),
            event_bus=self.event_bus,
        )

        for item in await ai_repo.list_overdue(limit=100):
            if item.get("overdue_notified_at") is not None:
                continue
            await dispatcher.notify(
                type="task_overdue",
                title=f"Overdue: {item['title']}",
                body=f"Was due {item.get('due_date') or 'earlier'}.",
                priority="low",
                reference_type="action_item",
                reference_id=item["id"],
            )
            await ai_repo.mark_overdue_notified(item["id"], now)

        for item in await ai_repo.list_due_reminders(limit=100):
            await dispatcher.notify(
                type="task_reminder",
                title=f"Reminder: {item['title']}",
                body=f"Due {item.get('due_date') or 'soon'}.",
                priority="low",
                reference_type="action_item",
                reference_id=item["id"],
            )
            await ai_repo.clear_reminder(item["id"])
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_reminders_producer.py tests/test_action_items.py -q
```

Expected: PASS (both new tests green; existing action-item tests unaffected — `limit` is a defaulted param).

- [ ] **Step 5: Commit**

```bash
git add src/action_items/repository.py src/api/server.py tests/test_reminders_producer.py
git commit -m "feat(notifications): transition-based overdue + reminder-clearing sweep

Notify once when an action item flips overdue (overdue_notified_at null->now),
clear reminder_at after a reminder fires, and bound list_overdue/list_due_reminders
with a LIMIT. task_overdue/task_reminder emit at priority 'low'."
```

---

### Task 11: Daily digest producer + scheduler registration

**Files:**

- Modify `src/api/server.py`: add module-level `_within_digest_window` helper (after `logger` at `:53`); add `_emit_daily_digest` and `_run_daily_digest` methods (after `_check_reminders`); register `daily_digest` inside the `if config.notifications.enabled:` block of `_setup_scheduler_jobs` (`:470`).
- Test (create) `tests/test_daily_digest.py`.

**Interfaces:**

- Consumes: `NotificationDispatcher.notify(...)`; `ActionItemRepository.list_items(status=..., limit=...)` and `list_overdue(limit=...)` (Task 10); `NotificationsConfig.task_digest` / `.digest_time` (config task); `Scheduler.register(name, func, interval_seconds)`; `safe_run`.
- Produces: `ApiServer._emit_daily_digest(self) -> None` (emits one `digest` notification, `dedup_key=f"digest:{YYYY-MM-DD}"`, priority `low`, body `"<n> due today, <m> overdue."`), `ApiServer._run_daily_digest(self) -> None` (digest-time-windowed wrapper), and a `daily_digest` scheduler job gated on `task_digest != "off"`. Task 13 later swaps the inline dispatcher build for `self._get_notification_dispatcher()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daily_digest.py
"""Daily digest: builds counts, emits one digest/day, disabled when off."""

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.action_items.repository import ActionItemRepository
from src.api.server import ApiServer
from src.notifications.dispatcher import NotificationDispatcher
from src.utils.config import NotificationsConfig


class _RecordingScheduler:
    def __init__(self):
        self.registered = []

    def register(self, name, func, interval):
        self.registered.append((name, interval))


def _sched_config(task_digest="daily"):
    return SimpleNamespace(
        notifications=SimpleNamespace(enabled=True, task_digest=task_digest),
        analytics=SimpleNamespace(refresh_interval_hours=6),
        series=SimpleNamespace(heuristic_enabled=False),
        calendar=SimpleNamespace(import_enabled=False, sync_interval_minutes=15),
        prep=SimpleNamespace(auto_generate=False, sweep_interval_minutes=15),
    )


@pytest.mark.asyncio
async def test_digest_notifies_with_counts(db, event_bus):
    server = ApiServer()
    server.db = db
    server.event_bus = event_bus
    ai_repo = ActionItemRepository(db)
    today = datetime.date.today().isoformat()
    await ai_repo.create(meeting_id="m1", title="Due today", due_date=today, status="open")
    await ai_repo.create(meeting_id="m1", title="Late", due_date="2020-01-01", status="open")

    cfg = SimpleNamespace(
        notifications=NotificationsConfig(
            enabled=True, in_app=True, macos=False, task_digest="daily"
        )
    )
    with patch.object(NotificationDispatcher, "notify", new=AsyncMock(return_value="n1")) as notify:
        with patch("src.api.server.load_config", return_value=cfg):
            await server._emit_daily_digest()

    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["type"] == "digest"
    assert kwargs["dedup_key"] == f"digest:{today}"
    assert kwargs["priority"] == "low"
    assert "1 due today" in kwargs["body"]
    assert "1 overdue" in kwargs["body"]


@pytest.mark.asyncio
async def test_digest_disabled_when_off(db, event_bus):
    server = ApiServer()
    server.db = db
    server.event_bus = event_bus
    cfg = SimpleNamespace(
        notifications=NotificationsConfig(enabled=True, task_digest="off")
    )
    with patch.object(NotificationDispatcher, "notify", new=AsyncMock()) as notify:
        with patch("src.api.server.load_config", return_value=cfg):
            await server._emit_daily_digest()
    notify.assert_not_awaited()


def test_daily_digest_registered_when_enabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_sched_config("daily")):
        server._setup_scheduler_jobs()
    assert "daily_digest" in [n for n, _ in server._scheduler.registered]


def test_daily_digest_absent_when_off():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_sched_config("off")):
        server._setup_scheduler_jobs()
    assert "daily_digest" not in [n for n, _ in server._scheduler.registered]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_daily_digest.py -q
```

Expected: FAIL. `AttributeError: 'ApiServer' object has no attribute '_emit_daily_digest'` for the two async tests, and the two registration tests fail `assert "daily_digest" in [...]` (no such job registered yet).

- [ ] **Step 3: Write minimal implementation**

In `src/api/server.py`, add the module-level helper immediately after `logger = logging.getLogger("contextrecall.api")` (`:53`):

```python
def _within_digest_window(digest_time: str, now: float, window_minutes: int = 20) -> bool:
    """True when local time at *now* falls within [digest_time, digest_time+window).

    The window is wider than the scheduler's check interval so at least one
    tick lands inside it; the digest's ``dedup_key`` keeps it to one per day.
    """
    import time as _time

    try:
        hh, mm = (int(x) for x in digest_time.split(":"))
    except (ValueError, AttributeError):
        return False
    lt = _time.localtime(now)
    current = lt.tm_hour * 60 + lt.tm_min
    target = hh * 60 + mm
    return target <= current < target + window_minutes
```

In `_setup_scheduler_jobs`, extend the existing `if config.notifications.enabled:` block (`:470`–`:475`) so it also registers the digest:

```python
        if config.notifications.enabled:
            self._scheduler.register(
                "reminder_check",
                lambda: safe_run("reminder_check", self._check_reminders),
                # Wire the real cadence: replaces the hard-coded 60s that made
                # the config "lie". Overdue is now transition-based (fires once),
                # so a slower sweep is correct; reminders fire within this window.
                max(60, config.notifications.overdue_recheck_minutes * 60),
            )
            if config.notifications.task_digest != "off":
                self._scheduler.register(
                    "daily_digest",
                    lambda: safe_run("daily_digest", self._run_daily_digest),
                    600,
                )
```

Add these two methods immediately after `_check_reminders`:

```python
    async def _emit_daily_digest(self) -> None:
        """Emit one daily digest summarising due-today and overdue action items.

        Idempotent per day via ``dedup_key=f"digest:{date}"`` — the scheduler
        may call this several times inside the digest window; only the first
        insert survives the DB unique index.
        """
        import datetime

        from src.action_items.repository import ActionItemRepository
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository

        config = load_config()
        if config.notifications.task_digest == "off":
            return

        ai_repo = ActionItemRepository(self.db)
        today = datetime.date.today().isoformat()
        active = await ai_repo.list_items(status="open", limit=1000)
        active += await ai_repo.list_items(status="in_progress", limit=1000)
        due_today = sum(1 for it in active if it.get("due_date") == today)
        overdue_count = len(await ai_repo.list_overdue(limit=1000))

        dispatcher = NotificationDispatcher(
            config=config.notifications,
            repo=NotificationRepository(self.db),
            event_bus=self.event_bus,
        )
        await dispatcher.notify(
            type="digest",
            title="Your daily task digest",
            body=f"{due_today} due today, {overdue_count} overdue.",
            priority="low",
            reference_type="action_item",
            dedup_key=f"digest:{today}",
        )

    async def _run_daily_digest(self) -> None:
        """Scheduler wrapper: only emit the digest inside the digest_time window."""
        import time

        config = load_config()
        if config.notifications.task_digest == "off":
            return
        if not _within_digest_window(config.notifications.digest_time, time.time()):
            return
        await self._emit_daily_digest()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_daily_digest.py tests/test_server_prep_sweep.py -q
```

Expected: PASS (all four digest tests green; the existing prep-sweep scheduler test still passes — the new job registers only when notifications are enabled with `task_digest != "off"`).

- [ ] **Step 5: Commit**

```bash
git add src/api/server.py tests/test_daily_digest.py
git commit -m "feat(notifications): daily task digest producer and scheduler job

Add _emit_daily_digest (one 'digest' notification per day, dedup_key digest:<date>,
summarising due-today + overdue counts) plus a digest-time-windowed scheduler job
gated on task_digest != 'off'."
```

---

### Task 12: Route automation `notify` through the governed dispatcher

**Files:**

- Modify `src/automations/executor.py:12` (drop the direct `macos_send` import), add `import time` (`:5` area), rewrite `_notify` (`:122`–`:126`).
- Modify `src/pipeline_runner.py:197` (`__init__` add `event_bus`), `:218` (`from_config` add + pass `event_bus`), `:1177`–`:1183` (`_run_automations` build dispatcher + add to `services`).
- Modify `src/main.py:850` (pass `event_bus=self._event_bus`) and `src/api/routes/reprocess.py:66` (pass `event_bus=_event_bus`).
- Test: replace `test_notify_emits_and_banners` (`tests/test_automations_executor.py:46`–`:54`) with a dispatcher-routing test.

**Interfaces:**

- Consumes: `NotificationDispatcher.notify(*, type, title, body, priority, reference_type, reference_id, dedup_key, ...)`; `NotificationRepository(db)`; `services["notification_dispatcher"]`.
- Produces: `ActionExecutor._notify` now routes to `services["notification_dispatcher"].notify(type="automation", priority="normal", reference_type="meeting", reference_id=meeting_id, dedup_key=f"automation:{rule.name}:{meeting_id}:{int(now // (cooldown*60))}")` and never banners directly; `PipelineRunner.__init__(..., event_bus=None)` / `from_config(..., event_bus=None)`; `_run_automations` injects a single per-run dispatcher.

- [ ] **Step 1: Write the failing test** (replace `test_notify_emits_and_banners` at `tests/test_automations_executor.py:46`–`54`)

```python
def test_notify_routes_through_dispatcher(monkeypatch):
    repo = MagicMock()
    calls = []

    class _FakeDispatcher:
        async def notify(self, **kwargs):
            calls.append(kwargs)
            return "notif-1"

    # Belt-and-suspenders: no direct macOS banner, regardless of TDD phase.
    monkeypatch.setattr("src.automations.executor.macos_send", AsyncMock(), raising=False)

    rule = {
        "name": "Weekly sync",
        "actions": [{"type": "notify", "message": "heads up", "notify_cooldown_minutes": 30}],
    }
    ex = ActionExecutor(
        repo,
        emit=lambda *a, **k: None,
        services={"notification_dispatcher": _FakeDispatcher()},
    )
    asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=True))

    assert len(calls) == 1
    call = calls[0]
    assert call["type"] == "automation"
    assert call["priority"] == "normal"
    assert call["reference_type"] == "meeting"
    assert call["reference_id"] == "m1"
    assert call["dedup_key"].startswith("automation:Weekly sync:m1:")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_automations_executor.py::test_notify_routes_through_dispatcher -q
```

Expected: FAIL with `assert 0 == 1` — the current `_notify` calls `self._emit` + `macos_send` and never touches `services["notification_dispatcher"]`, so `calls` stays empty. (The `monkeypatch` neutralises the real `macos_send`, so no `osascript` runs during the red phase.)

- [ ] **Step 3: Write minimal implementation**

In `src/automations/executor.py`: add `import time` to the top imports and delete `from src.notifications.channels.macos import send as macos_send` (`:12`). Replace `_notify` (`:122`–`:126`):

```python
    async def _notify(self, action: dict, context: dict, rule: dict, meeting_id: str) -> None:
        dispatcher = self._services.get("notification_dispatcher")
        if dispatcher is None:
            return
        title = context.get("title") or "Context Recall"
        body = action.get("message") or f"Automation '{rule.get('name')}' matched."
        cooldown = action.get("notify_cooldown_minutes") or 60  # minutes; guards div-by-zero
        now = time.time()
        dedup_key = f"automation:{rule.get('name')}:{meeting_id}:{int(now // (cooldown * 60))}"
        await dispatcher.notify(
            type="automation",
            title=title,
            body=body,
            priority="normal",
            reference_type="meeting",
            reference_id=meeting_id,
            dedup_key=dedup_key,
        )
```

In `src/pipeline_runner.py` `__init__` (`:197`–`:216`), add the `event_bus` keyword-only param and its `self._event_bus` assignment — **keep every existing assignment** (`self._transcriber` … `self._notion_writer`). The complete method:

```python
    def __init__(
        self,
        config,
        *,
        emit=None,
        event_bus=None,
        db: DbBridge | None = None,
        transcriber: Transcriber | None = None,
        summariser: Summariser | None = None,
        diariser=None,
        md_writer: MarkdownWriter | None = None,
        notion_writer: NotionWriter | None = None,
    ) -> None:
        self._config = config
        self._emit_cb = emit
        self._event_bus = event_bus
        self._db = db
        self._transcriber = transcriber or Transcriber(config.transcription)
        self._summariser = summariser or Summariser(config.summarisation)
        self._diariser = diariser
        self._md_writer = md_writer
        self._notion_writer = notion_writer
```

In `from_config` (`:219`; the `@classmethod` decorator is `:218`) add the param and forward it:

```python
    @classmethod
    def from_config(
        cls, config, *, emit=None, event_bus=None, db: DbBridge | None = None
    ) -> "PipelineRunner":
        diariser = create_diariser(config.diarisation) if config.diarisation.enabled else None
        md_writer = MarkdownWriter(config.markdown) if config.markdown.enabled else None
        notion_writer = NotionWriter(config.notion) if config.notion.enabled else None
        return cls(
            config,
            emit=emit,
            event_bus=event_bus,
            db=db,
            diariser=diariser,
            md_writer=md_writer,
            notion_writer=notion_writer,
        )
```

In `_run_automations` (`:1177`) build one dispatcher per run and inject it:

```python
        from src.action_items.repository import ActionItemRepository
        from src.insights.repository import InsightRepository
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository

        services = {
            "meeting": meeting,
            "insight_repo": InsightRepository(self._db.database),
            "action_items_repo": ActionItemRepository(self._db.database),
            "summarisation_config": self._config.summarisation,
            "notification_dispatcher": NotificationDispatcher(
                config=self._config.notifications,
                repo=NotificationRepository(self._db.database),
                event_bus=self._event_bus,
            ),
        }
```

In `src/main.py` (`:850`), pass the orchestrator's bus:

```python
        runner = PipelineRunner(
            self._config,
            emit=self._emit,
            event_bus=self._event_bus,
            db=_OrchestratorDbBridge(self),
            transcriber=self._transcriber,
            summariser=self._summariser,
            diariser=self._diariser,
            md_writer=self._md_writer,
            notion_writer=self._notion_writer,
        )
```

In `src/api/routes/reprocess.py` `_make_runner` (`:66`):

```python
    return PipelineRunner.from_config(config, emit=emit, event_bus=_event_bus, db=bridge)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_automations_executor.py tests/test_automation_actions.py tests/test_pipeline_runner.py -q
```

Expected: PASS (dispatcher-routing test green; automations/pipeline suites unaffected — `event_bus` is a defaulted keyword-only param).

- [ ] **Step 5: Commit**

```bash
git add src/automations/executor.py src/pipeline_runner.py src/main.py src/api/routes/reprocess.py tests/test_automations_executor.py
git commit -m "feat(automations): route notify action through the notification dispatcher

Replace the direct, ungoverned macos_send in ActionExecutor._notify with
dispatcher.notify(type='automation', ...) using a per-rule cooldown-bucketed
dedup_key. Thread the event bus into PipelineRunner and inject a single
per-run dispatcher via services['notification_dispatcher']."
```

---

### Task 13: Retention prune task + single reused dispatcher instance

**Files:**

- Modify `src/api/server.py`: `__init__` (`:79`) add `self._notif_dispatcher = None`; add `_get_notification_dispatcher` + `_prune_notifications` methods; register `notification_prune` at the end of `_setup_scheduler_jobs` (`:502`); refactor `_check_reminders` (Task 10) and `_emit_daily_digest` (Task 11) to reuse `self._get_notification_dispatcher()`.
- Test (create) `tests/test_notification_retention.py`.

**Interfaces:**

- Consumes: `NotificationRepository.prune(*, now: float, retention_days: int, dismissed_retention_days: int, max_rows: int) -> int` (storage task); `NotificationsConfig.retention_days/.dismissed_retention_days/.max_rows` (config task); `NotificationDispatcher.__init__(config, repo, event_bus)`.
- Produces: `ApiServer._prune_notifications(self) -> None`; `ApiServer._get_notification_dispatcher(self) -> NotificationDispatcher` (built once, cached on `self._notif_dispatcher`, reused by `_check_reminders`/`_emit_daily_digest`); a `notification_prune` scheduler job (every 6h). Note: the cached dispatcher captures config at first build — config edits take effect on daemon restart (accepted Phase-1 tradeoff).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notification_retention.py
"""Retention prune task + single reused dispatcher instance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.api.server import ApiServer
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.repository import NotificationRepository
from src.utils.config import NotificationsConfig


@pytest.mark.asyncio
async def test_prune_task_calls_repo_with_configured_windows(db):
    server = ApiServer()
    server.db = db
    cfg = SimpleNamespace(
        notifications=NotificationsConfig(
            enabled=True, retention_days=30, dismissed_retention_days=7, max_rows=500
        )
    )
    with patch.object(NotificationRepository, "prune", new=AsyncMock(return_value=4)) as prune:
        with patch("src.api.server.load_config", return_value=cfg):
            await server._prune_notifications()

    prune.assert_awaited_once()
    kwargs = prune.await_args.kwargs
    assert kwargs["retention_days"] == 30
    assert kwargs["dismissed_retention_days"] == 7
    assert kwargs["max_rows"] == 500
    assert "now" in kwargs


@pytest.mark.asyncio
async def test_dispatcher_built_once_and_reused(db, event_bus):
    server = ApiServer()
    server.db = db
    server.event_bus = event_bus
    cfg = SimpleNamespace(notifications=NotificationsConfig(enabled=True, in_app=True, macos=False))
    with patch("src.api.server.load_config", return_value=cfg):
        d1 = server._get_notification_dispatcher()
        d2 = server._get_notification_dispatcher()
    assert isinstance(d1, NotificationDispatcher)
    assert d1 is d2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_notification_retention.py -q
```

Expected: FAIL — `AttributeError: 'ApiServer' object has no attribute '_prune_notifications'` and `... no attribute '_get_notification_dispatcher'`.

- [ ] **Step 3: Write minimal implementation**

In `src/api/server.py` `__init__`, add after `self._retention_task: asyncio.Task | None = None` (`:79`):

```python
        self._notif_dispatcher = None
```

Add these two methods (e.g. directly before `_check_reminders`):

```python
    def _get_notification_dispatcher(self):
        """Return the process-wide dispatcher, building it once and reusing it.

        Config is captured at first build; changes take effect on restart.
        """
        if self._notif_dispatcher is None:
            from src.notifications.dispatcher import NotificationDispatcher
            from src.notifications.repository import NotificationRepository

            self._notif_dispatcher = NotificationDispatcher(
                config=load_config().notifications,
                repo=NotificationRepository(self.db),
                event_bus=self.event_bus,
            )
        return self._notif_dispatcher

    async def _prune_notifications(self) -> None:
        """Delete stale notification rows per the configured retention policy."""
        import time

        from src.notifications.repository import NotificationRepository

        cfg = load_config().notifications
        repo = NotificationRepository(self.db)
        deleted = await repo.prune(
            now=time.time(),
            retention_days=cfg.retention_days,
            dismissed_retention_days=cfg.dismissed_retention_days,
            max_rows=cfg.max_rows,
        )
        if deleted:
            logger.info("Pruned %d stale notification(s)", deleted)
```

Register the prune job at the end of `_setup_scheduler_jobs` (after the prep block, unconditional — cleanup must run even when notifications are muted):

```python
        self._scheduler.register(
            "notification_prune",
            lambda: safe_run("notification_prune", self._prune_notifications),
            6 * 3600,
        )
```

Refactor `_check_reminders` (from Task 10) to reuse the single instance — replace its import/construction header:

```python
        import time

        from src.action_items.repository import ActionItemRepository
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository

        config = load_config()
        now = time.time()
        ai_repo = ActionItemRepository(self.db)
        dispatcher = NotificationDispatcher(
            config=config.notifications,
            repo=NotificationRepository(self.db),
            event_bus=self.event_bus,
        )
```

with:

```python
        import time

        from src.action_items.repository import ActionItemRepository

        now = time.time()
        ai_repo = ActionItemRepository(self.db)
        dispatcher = self._get_notification_dispatcher()
```

Refactor `_emit_daily_digest` (from Task 11) likewise — replace its dispatcher construction:

```python
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository
        ...
        dispatcher = NotificationDispatcher(
            config=config.notifications,
            repo=NotificationRepository(self.db),
            event_bus=self.event_bus,
        )
```

with (keep `config = load_config()` and the `task_digest == "off"` guard as-is; drop the now-unused imports):

```python
        dispatcher = self._get_notification_dispatcher()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_notification_retention.py tests/test_reminders_producer.py tests/test_daily_digest.py -q
```

Expected: PASS — prune calls `repo.prune` with the configured windows; `_get_notification_dispatcher` returns the same instance twice; the Task 10/11 producer suites stay green through the single-instance refactor.

- [ ] **Step 5: Commit**

```bash
git add src/api/server.py tests/test_notification_retention.py
git commit -m "feat(notifications): retention prune job and single reused dispatcher

Add a 6-hourly _prune_notifications job that calls repo.prune with the configured
retention/dismissed-retention/max-rows windows, and build one NotificationDispatcher
cached on the server, reused by _check_reminders and _emit_daily_digest instead of
reconstructing it every tick."
```

---

### Task 14: Notifications API — bulk actions, PATCH read/dismiss/snooze, list filters

**Files:**

- Modify: `src/api/routes/notifications.py` (full rewrite of the 43-line file; current `list_notifications` uses a `status` query param and only a `dismiss` PATCH)
- Replace (test): `tests/test_api_notifications.py` — this **supersedes** the Phase-0 v24 version written in Task 2 (which becomes red once Task 4 rewrites `create`; it is intentionally replaced here with v25-aware coverage).

**Interfaces:**

- Consumes (from the Storage cluster's `NotificationRepository`, `src/notifications/repository.py`): `create(*, type, title, body, priority, channels, status="unread", reference_type=None, reference_id=None, dedup_key=None, group_key=None) -> str | None`; `list_notifications(*, limit=50, offset=0, type=None, include_dismissed=False) -> list[dict]`; `count_unread(now=None) -> int`; `mark_read(id)`; `mark_all_read(*, type=None) -> int`; `dismiss(id)`; `dismiss_all(*, type=None) -> int`; `snooze(id, until)`. Requires the v25 migration (Storage cluster) so the `notifications` table has the new columns.
- Produces (consumed by Task 15 UI api layer): `GET /api/notifications?limit&offset&type&include_dismissed` → `{notifications: [...]}`; `GET /api/notifications/unread-count` → `{count:int}`; `POST /api/notifications/read-all` body `{type?}` → `{updated:int}`; `POST /api/notifications/clear-all` body `{type?}` → `{updated:int}`; `PATCH /api/notifications/{id}` body `{action:'read'|'dismiss'|'snooze', snooze_minutes?}`.

- [ ] **Step 1: Write the failing test** — `tests/test_api_notifications.py`

```python
"""Tests for src/api/routes/notifications.py — bulk actions, PATCH actions, list filters."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import notifications as notif_routes
from src.db.database import Database
from src.notifications.repository import NotificationRepository


@pytest.fixture
async def notif_client(tmp_path):
    db = Database(db_path=tmp_path / "notif_api.db")
    await db.connect()
    repo = NotificationRepository(db)
    notif_routes.init(repo)

    app = FastAPI()
    app.include_router(notif_routes.router)
    with TestClient(app) as client:
        yield client, repo
    await db.close()


async def _seed(repo, **kw):
    return await repo.create(
        type=kw.get("type", "meeting_processed"),
        title=kw.get("title", "Notes ready"),
        body=kw.get("body", "Your meeting notes are ready"),
        priority=kw.get("priority", "normal"),
        channels=kw.get("channels", ["in_app"]),
        status=kw.get("status", "unread"),
        reference_type=kw.get("reference_type"),
        reference_id=kw.get("reference_id"),
        dedup_key=kw.get("dedup_key"),
        group_key=kw.get("group_key"),
    )


@pytest.mark.asyncio
async def test_read_all_marks_everything_read(notif_client):
    client, repo = notif_client
    await _seed(repo, dedup_key="a")
    await _seed(repo, dedup_key="b")
    resp = client.post("/api/notifications/read-all", json={})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2
    assert await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_read_all_filtered_by_type(notif_client):
    client, repo = notif_client
    await _seed(repo, type="meeting_processed", dedup_key="a")
    await _seed(repo, type="task_overdue", dedup_key="b")
    resp = client.post("/api/notifications/read-all", json={"type": "task_overdue"})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    assert await repo.count_unread() == 1


@pytest.mark.asyncio
async def test_clear_all_dismisses(notif_client):
    client, repo = notif_client
    await _seed(repo, dedup_key="a")
    resp = client.post("/api/notifications/clear-all", json={})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 1
    items = await repo.list_notifications(include_dismissed=True)
    assert items[0]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_patch_read_action(notif_client):
    client, repo = notif_client
    nid = await _seed(repo, dedup_key="a")
    resp = client.patch(f"/api/notifications/{nid}", json={"action": "read"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "read"
    assert await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_patch_dismiss_action(notif_client):
    client, repo = notif_client
    nid = await _seed(repo, dedup_key="a")
    resp = client.patch(f"/api/notifications/{nid}", json={"action": "dismiss"})
    assert resp.status_code == 200
    items = await repo.list_notifications(include_dismissed=True)
    assert items[0]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_patch_snooze_sets_snoozed_until(notif_client):
    client, repo = notif_client
    nid = await _seed(repo, dedup_key="a")
    resp = client.patch(
        f"/api/notifications/{nid}", json={"action": "snooze", "snooze_minutes": 30}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unread"
    assert body["snoozed_until"] > time.time()
    # A snoozed item is not counted as unread until its snooze expires.
    assert await repo.count_unread() == 0


@pytest.mark.asyncio
async def test_patch_unknown_action_422(notif_client):
    client, repo = notif_client
    nid = await _seed(repo, dedup_key="a")
    resp = client.patch(f"/api/notifications/{nid}", json={"action": "bogus"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_excludes_dismissed_by_default(notif_client):
    client, repo = notif_client
    await _seed(repo, dedup_key="a", title="Keep")
    drop = await _seed(repo, dedup_key="b", title="Drop")
    await repo.dismiss(drop)
    resp = client.get("/api/notifications")
    assert [n["title"] for n in resp.json()["notifications"]] == ["Keep"]
    resp2 = client.get("/api/notifications?include_dismissed=true")
    assert len(resp2.json()["notifications"]) == 2


@pytest.mark.asyncio
async def test_list_filters_by_type(notif_client):
    client, repo = notif_client
    await _seed(repo, type="meeting_processed", dedup_key="a", title="M")
    await _seed(repo, type="task_overdue", dedup_key="b", title="T")
    resp = client.get("/api/notifications?type=task_overdue")
    assert [n["title"] for n in resp.json()["notifications"]] == ["T"]
```

- [ ] **Step 2: Run test to verify it fails**

```
python -m pytest tests/test_api_notifications.py -q
```

Expected failure: the current route has no `/read-all` or `/clear-all` (POST returns **404 Not Found**), the PATCH handler reads `body.status` and does not understand `action` (`test_patch_*` fail), and `list_notifications` still takes a `status=` param so `include_dismissed`/`type` filters are ignored.

- [ ] **Step 3: Write minimal implementation** — replace the entire contents of `src/api/routes/notifications.py`

```python
"""API routes for notification management."""

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.notifications.repository import NotificationRepository

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
_repo: NotificationRepository | None = None


def init(repo: NotificationRepository) -> None:
    global _repo
    _repo = repo


def _get_repo() -> NotificationRepository:
    if _repo is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _repo


class BulkRequest(BaseModel):
    type: str | None = None


class NotificationActionRequest(BaseModel):
    action: str
    snooze_minutes: int | None = None


@router.get("")
async def list_notifications(
    limit: int = 50,
    offset: int = 0,
    type: str | None = None,
    include_dismissed: bool = False,
):
    items = await _get_repo().list_notifications(
        limit=limit,
        offset=offset,
        type=type,
        include_dismissed=include_dismissed,
    )
    return {"notifications": items}


@router.get("/unread-count")
async def unread_count():
    count = await _get_repo().count_unread()
    return {"count": count}


@router.post("/read-all")
async def read_all(body: BulkRequest):
    updated = await _get_repo().mark_all_read(type=body.type)
    return {"updated": updated}


@router.post("/clear-all")
async def clear_all(body: BulkRequest):
    updated = await _get_repo().dismiss_all(type=body.type)
    return {"updated": updated}


@router.patch("/{notif_id}")
async def update_notification(notif_id: str, body: NotificationActionRequest):
    repo = _get_repo()
    if body.action == "read":
        await repo.mark_read(notif_id)
        return {"status": "read"}
    if body.action == "dismiss":
        await repo.dismiss(notif_id)
        return {"status": "dismissed"}
    if body.action == "snooze":
        minutes = body.snooze_minutes if body.snooze_minutes is not None else 60
        until = time.time() + minutes * 60
        await repo.snooze(notif_id, until)
        return {"status": "unread", "snoozed_until": until}
    raise HTTPException(status_code=422, detail=f"Unknown action: {body.action}")
```

- [ ] **Step 4: Run test to verify it passes**

```
python -m pytest tests/test_api_notifications.py -q
```

Expected: **9 passed**.

- [ ] **Step 5: Commit**

```
git add src/api/routes/notifications.py tests/test_api_notifications.py
git commit -m "feat(api): add notification bulk read/clear, PATCH actions and list filters"
```

---

### Task 15: UI api layer + notification types

**Files:**

- Modify: `ui/src/lib/types.ts:577` (`NotificationStatus`, `AppNotification`) and `:301` (`NotificationsConfig`)
- Modify: `ui/src/lib/api.ts:856` (`getNotifications`, `dismissNotification`) — add `markNotificationRead`, `snoozeNotification`, `markAllRead`, `clearAllNotifications`
- Create (test): `ui/src/lib/__tests__/notifications.api.test.ts`

**Interfaces:**

- Consumes (from Task 14): the five REST endpoints and their JSON shapes.
- Produces (consumed by Tasks 16 & 17): `getNotifications(limit=50, opts?:{type?; includeDismissed?}) -> Promise<NotificationsResponse>`; `markNotificationRead(id) -> Promise<void>`; `dismissNotification(id) -> Promise<void>`; `snoozeNotification(id, minutes) -> Promise<void>`; `markAllRead(type?) -> Promise<{updated:number}>`; `clearAllNotifications(type?) -> Promise<{updated:number}>`; `AppNotification` with `priority/reference_type/channels/group_key/read_at/snoozed_until/status`; extended `NotificationsConfig` type (used by Task 17).

- [ ] **Step 1: Write the failing test** — `ui/src/lib/__tests__/notifications.api.test.ts`

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  setAuthToken,
  getNotifications,
  markAllRead,
  clearAllNotifications,
  dismissNotification,
  markNotificationRead,
  snoozeNotification,
} from "../api";

const originalFetch = globalThis.fetch;

beforeEach(() => setAuthToken("t"));
afterEach(() => {
  globalThis.fetch = originalFetch;
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("notifications api", () => {
  it("getNotifications passes limit, type and include_dismissed", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return json({ notifications: [] });
    }) as unknown as typeof fetch;

    await getNotifications(25, {
      type: "meeting_failed",
      includeDismissed: true,
    });
    expect(calls[0]).toContain("/api/notifications?");
    expect(calls[0]).toContain("limit=25");
    expect(calls[0]).toContain("type=meeting_failed");
    expect(calls[0]).toContain("include_dismissed=true");
  });

  it("getNotifications omits optional params when not given", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return json({ notifications: [] });
    }) as unknown as typeof fetch;

    await getNotifications();
    expect(calls[0]).toContain("limit=50");
    expect(calls[0]).not.toContain("type=");
    expect(calls[0]).not.toContain("include_dismissed");
  });

  it("markAllRead POSTs the type filter", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ updated: 3 });
      },
    ) as unknown as typeof fetch;

    const res = await markAllRead("task_overdue");
    expect(res.updated).toBe(3);
    const call = calls.find((c) => c.init?.method === "POST")!;
    expect(call.url).toContain("/api/notifications/read-all");
    expect(JSON.parse(call.init?.body as string)).toEqual({
      type: "task_overdue",
    });
  });

  it("markAllRead sends an empty body when no type", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ updated: 0 });
      },
    ) as unknown as typeof fetch;

    await markAllRead();
    const call = calls.find((c) => c.init?.method === "POST")!;
    expect(JSON.parse(call.init?.body as string)).toEqual({});
  });

  it("clearAllNotifications POSTs to clear-all", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ updated: 5 });
      },
    ) as unknown as typeof fetch;

    const res = await clearAllNotifications();
    expect(res.updated).toBe(5);
    const call = calls.find((c) => c.init?.method === "POST")!;
    expect(call.url).toContain("/api/notifications/clear-all");
  });

  it("dismissNotification PATCHes action=dismiss", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ status: "dismissed" });
      },
    ) as unknown as typeof fetch;

    await dismissNotification("n1");
    expect(calls[0].url).toContain("/api/notifications/n1");
    expect(calls[0].init?.method).toBe("PATCH");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      action: "dismiss",
    });
  });

  it("markNotificationRead PATCHes action=read", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ status: "read" });
      },
    ) as unknown as typeof fetch;

    await markNotificationRead("n2");
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      action: "read",
    });
  });

  it("snoozeNotification PATCHes action=snooze with minutes", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return json({ status: "unread", snoozed_until: 123 });
      },
    ) as unknown as typeof fetch;

    await snoozeNotification("n3", 30);
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      action: "snooze",
      snooze_minutes: 30,
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ui && npx vitest run src/lib/__tests__/notifications.api.test.ts
```

Expected failure: TypeScript/import errors — `markAllRead`, `clearAllNotifications`, `snoozeNotification`, `markNotificationRead` are not exported from `../api`, and `getNotifications` does not accept an `opts` argument.

- [ ] **Step 3: Write minimal implementation**

First, `ui/src/lib/types.ts` — replace the Notification types block (lines 576-598):

```ts
/** Notification types. */
export type NotificationStatus = "unread" | "read" | "dismissed" | "failed";

export interface AppNotification {
  id: string;
  type: string;
  priority: string;
  reference_type: string | null;
  reference_id: string | null;
  channels: string[];
  group_key: string | null;
  title: string;
  body: string | null;
  status: NotificationStatus;
  read_at: number | null;
  snoozed_until: number | null;
  sent_at: number | null;
  created_at: number;
}

export interface NotificationsResponse {
  notifications: AppNotification[];
}

export interface UnreadCountResponse {
  count: number;
}
```

Then replace the `NotificationsConfig` interface (lines 301-309) with the extended shape (consumed by Task 17):

```ts
export interface NotificationsConfig {
  enabled: boolean;
  in_app: boolean;
  macos: boolean;
  macos_sound: boolean;
  webhook: WebhookChannelConfig;
  email: EmailChannelConfig;
  muted_types: string[];
  macos_min_priority: string;
  external_min_priority: string;
  max_per_hour: number;
  per_type_max_per_hour: Record<string, number>;
  quiet_hours_enabled: boolean;
  quiet_start: string;
  quiet_end: string;
  task_digest: string;
  digest_time: string;
  overdue_recheck_minutes: number;
  dedup_window_minutes: number;
  retention_days: number;
  dismissed_retention_days: number;
  max_rows: number;
  default_reminder_before_due: string;
  overdue_check_interval: string;
}
```

Then in `ui/src/lib/api.ts`, replace the `// --- Notifications ---` block (lines 854-871) with:

```ts
// --- Notifications ---

export async function getNotifications(
  limit = 50,
  opts?: { type?: string; includeDismissed?: boolean },
): Promise<NotificationsResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (opts?.type) params.set("type", opts.type);
  if (opts?.includeDismissed) params.set("include_dismissed", "true");
  return request<NotificationsResponse>(`/api/notifications?${params}`);
}

export async function getUnreadCount(): Promise<UnreadCountResponse> {
  return request<UnreadCountResponse>("/api/notifications/unread-count");
}

export async function markNotificationRead(id: string): Promise<void> {
  await request(`/api/notifications/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ action: "read" }),
  });
}

export async function dismissNotification(id: string): Promise<void> {
  await request(`/api/notifications/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ action: "dismiss" }),
  });
}

export async function snoozeNotification(
  id: string,
  minutes: number,
): Promise<void> {
  await request(`/api/notifications/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ action: "snooze", snooze_minutes: minutes }),
  });
}

export async function markAllRead(type?: string): Promise<{ updated: number }> {
  return request<{ updated: number }>("/api/notifications/read-all", {
    method: "POST",
    body: JSON.stringify(type ? { type } : {}),
  });
}

export async function clearAllNotifications(
  type?: string,
): Promise<{ updated: number }> {
  return request<{ updated: number }>("/api/notifications/clear-all", {
    method: "POST",
    body: JSON.stringify(type ? { type } : {}),
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ui && npx vitest run src/lib/__tests__/notifications.api.test.ts && npx tsc --noEmit
```

Expected: **8 passed** and no type errors.

- [ ] **Step 5: Commit**

```
git add ui/src/lib/api.ts ui/src/lib/types.ts ui/src/lib/__tests__/notifications.api.test.ts
git commit -m "feat(ui): notification api helpers (mark-all/clear-all/snooze/read) and richer types"
```

---

### Task 16: NotificationPanel + Badge + appStore — bulk actions, filter chips, mark-visible-read, single-source badge

**Files:**

- Modify: `ui/src/components/notifications/NotificationPanel.tsx` (full rewrite of the 173-line component)
- Modify: `ui/src/stores/appStore.ts:77-80` (interface) and `:114-117` (implementation) — add `decrementNotifications`
- Modify: `ui/src/stores/appStore.ts:218-221` and `ui/src/App.tsx:118` — **single-source the badge** (spec 5.7). Today the WS `case "notification"` handler does an unconditional `unreadNotifications + 1`, which drifts against the 30s poll. Remove that raw `+1` (delete the `set((state) => ({ unreadNotifications: state.unreadNotifications + 1 }))` in the `"notification"` case, leaving it a no-op) and instead, in `App.tsx`'s existing `event.type === "notification"` branch (`:118`), invalidate the react-query keys so the count refetches from the one source: `queryClient.invalidateQueries({ queryKey: ["notifications-unread"] })` and `["notifications"]`. Also remove the now-dead `incrementNotifications` action (interface `:79`, impl `:115`) — it has no callers after this. The badge's only writers become the `["notifications-unread"]` query (via `setUnreadNotifications`) and the optimistic `decrementNotifications`.
- `ui/src/components/notifications/NotificationBadge.tsx` stays as-is (it already derives from the single `appStore.unreadNotifications` source); covered by the test below
- Create (test): `ui/src/components/notifications/__tests__/NotificationPanel.test.tsx`

**Interfaces:**

- Consumes (from Task 15): `getNotifications`, `getUnreadCount`, `dismissNotification`, `markAllRead`, `clearAllNotifications`, `AppNotification`. Consumes `appStore` `setUnreadNotifications` and the new `decrementNotifications`.
- Produces: the redesigned panel (bulk Mark-all-read / Clear-all, filter chips, mark-visible-read on open, hidden dismissed rows) and the `decrementNotifications` store action.

- [ ] **Step 1: Write the failing test** — `ui/src/components/notifications/__tests__/NotificationPanel.test.tsx`

```tsx
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";

import { NotificationPanel } from "../NotificationPanel";
import { NotificationBadge } from "../NotificationBadge";
import { useAppStore } from "../../../stores/appStore";
import { makeWrapper } from "../../../test/queryWrapper";

interface Row {
  id: string;
  type: string;
  title: string;
  status: string;
  created_at: number;
  body: string | null;
  priority: string;
  reference_type: string | null;
  reference_id: string | null;
  channels: string[];
  group_key: string | null;
  read_at: number | null;
  snoozed_until: number | null;
  sent_at: number | null;
}

function row(partial: Partial<Row>): Row {
  return {
    id: "n1",
    type: "meeting_processed",
    title: "Notes ready",
    status: "unread",
    created_at: 1_600_000_000, // fixed epoch — deterministic test input
    body: null,
    priority: "normal",
    reference_type: null,
    reference_id: null,
    channels: ["in_app"],
    group_key: null,
    read_at: null,
    snoozed_until: null,
    sent_at: null,
    ...partial,
  };
}

/** A stateful fetch stub that mutates an in-memory row set like the real API. */
function installFetch(initial: Row[]) {
  const rows = [...initial];
  const json = (b: unknown, status = 200) =>
    new Response(JSON.stringify(b), {
      status,
      headers: { "content-type": "application/json" },
    });

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString();
      const method = init?.method ?? "GET";

      if (url.includes("/api/notifications/unread-count")) {
        return json({
          count: rows.filter((r) => r.status === "unread").length,
        });
      }
      if (url.includes("/api/notifications/read-all") && method === "POST") {
        let updated = 0;
        for (const r of rows)
          if (r.status === "unread") {
            r.status = "read";
            updated++;
          }
        return json({ updated });
      }
      if (url.includes("/api/notifications/clear-all") && method === "POST") {
        let updated = 0;
        for (const r of rows)
          if (r.status !== "dismissed") {
            r.status = "dismissed";
            updated++;
          }
        return json({ updated });
      }
      const patch = url.match(/\/api\/notifications\/([^/?]+)$/);
      if (patch && method === "PATCH") {
        const id = decodeURIComponent(patch[1]);
        const body = JSON.parse((init?.body as string) || "{}");
        const target = rows.find((r) => r.id === id);
        if (target && body.action === "dismiss") target.status = "dismissed";
        if (target && body.action === "read") target.status = "read";
        return json({ status: body.action });
      }
      if (url.includes("/api/notifications")) {
        return json({
          notifications: rows.filter((r) => r.status !== "dismissed"),
        });
      }
      return json({});
    },
  );
  globalThis.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function openPanel() {
  fireEvent(window, new Event("toggle-notifications"));
}

describe("NotificationPanel", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => useAppStore.setState({ unreadNotifications: 0 }));
  afterEach(() => {
    globalThis.fetch = originalFetch;
    useAppStore.setState({ unreadNotifications: 0 });
  });

  it("marks all read and zeroes the badge", async () => {
    const fetchMock = installFetch([
      row({ id: "a", title: "Notes ready" }),
      row({ id: "b", type: "task_overdue", title: "Task overdue" }),
    ]);
    useAppStore.setState({ unreadNotifications: 2 });

    render(<NotificationPanel />, { wrapper: makeWrapper() });
    openPanel();
    await screen.findByText("Notes ready");

    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([u, i]) =>
            u.toString().includes("/read-all") &&
            (i as RequestInit)?.method === "POST",
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(useAppStore.getState().unreadNotifications).toBe(0),
    );
  });

  it("filter chips filter the visible rows", async () => {
    installFetch([
      row({ id: "a", type: "meeting_processed", title: "Notes ready" }),
      row({ id: "b", type: "task_overdue", title: "Task overdue" }),
    ]);

    render(<NotificationPanel />, { wrapper: makeWrapper() });
    openPanel();
    await screen.findByText("Notes ready");
    expect(screen.getByText("Task overdue")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Meetings" }));

    expect(screen.getByText("Notes ready")).toBeInTheDocument();
    expect(screen.queryByText("Task overdue")).not.toBeInTheDocument();
  });

  it("dismiss removes a row", async () => {
    installFetch([
      row({ id: "a", title: "Notes ready" }),
      row({ id: "b", type: "insight", title: "Risk flagged" }),
    ]);

    render(<NotificationPanel />, { wrapper: makeWrapper() });
    openPanel();
    await screen.findByText("Risk flagged");

    const riskRow = screen.getByText("Risk flagged").closest("li")!;
    fireEvent.click(
      within(riskRow).getByRole("button", { name: "Dismiss notification" }),
    );

    await waitFor(() =>
      expect(screen.queryByText("Risk flagged")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Notes ready")).toBeInTheDocument();
  });

  it("NotificationBadge reflects the single store source", () => {
    useAppStore.setState({ unreadNotifications: 3 });
    const { rerender } = render(<NotificationBadge />, {
      wrapper: makeWrapper(),
    });
    expect(screen.getByText("3")).toBeInTheDocument();

    useAppStore.setState({ unreadNotifications: 0 });
    rerender(<NotificationBadge />);
    expect(screen.queryByText("3")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ui && npx vitest run src/components/notifications/__tests__/NotificationPanel.test.tsx
```

Expected failure: the current panel has no "Mark all read" button and no filter chips (`getByRole("button", { name: "Mark all read" })` / `"Meetings"` throw), and `useAppStore` has no `decrementNotifications` (the rewritten panel references it).

- [ ] **Step 3: Write minimal implementation**

First, extend `ui/src/stores/appStore.ts`. In the `AppState` interface (after line 79):

```ts
  /** Unread notification count. */
  unreadNotifications: number;
  incrementNotifications: () => void;
  decrementNotifications: (n?: number) => void;
  setUnreadNotifications: (count: number) => void;
```

And in the store body (after the `setUnreadNotifications` implementation, ~line 117):

```ts
  unreadNotifications: 0,
  incrementNotifications: () =>
    set((state) => ({ unreadNotifications: state.unreadNotifications + 1 })),
  decrementNotifications: (n = 1) =>
    set((state) => ({
      unreadNotifications: Math.max(0, state.unreadNotifications - n),
    })),
  setUnreadNotifications: (count) => set({ unreadNotifications: count }),
```

Then replace the entire contents of `ui/src/components/notifications/NotificationPanel.tsx`:

```tsx
import { useCallback, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";

import {
  getNotifications,
  getUnreadCount,
  dismissNotification,
  markAllRead,
  clearAllNotifications,
} from "../../lib/api";
import { useAppStore } from "../../stores/appStore";
import type { AppNotification } from "../../lib/types";

/** Maps a notification type to the filter-chip category it belongs to. */
const CATEGORY: Record<string, string> = {
  meeting_processed: "meetings",
  meeting_failed: "failures",
  prep_ready: "calendar",
  insight: "insights",
  automation: "insights",
  task_overdue: "tasks",
  task_reminder: "tasks",
  digest: "tasks",
};

const CHIPS: { id: string; label: string }[] = [
  { id: "all", label: "All" },
  { id: "meetings", label: "Meetings" },
  { id: "failures", label: "Failures" },
  { id: "insights", label: "Insights" },
  { id: "calendar", label: "Calendar" },
  { id: "tasks", label: "Tasks" },
];

function CloseIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

export function NotificationPanel() {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("all");
  const queryClient = useQueryClient();
  const setUnreadNotifications = useAppStore((s) => s.setUnreadNotifications);
  const decrementNotifications = useAppStore((s) => s.decrementNotifications);

  // Listen for the global toggle-notifications custom event.
  useEffect(() => {
    const handler = () => setOpen((prev) => !prev);
    window.addEventListener("toggle-notifications", handler);
    return () => window.removeEventListener("toggle-notifications", handler);
  }, []);

  // Fetch notifications (only when the panel is open). The API already
  // excludes dismissed rows by default.
  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => getNotifications(100),
    enabled: open,
    refetchInterval: open ? 10_000 : false,
  });

  // Poll the unread count into the single store source.
  useQuery({
    queryKey: ["notifications-unread"],
    queryFn: async () => {
      const res = await getUnreadCount();
      setUnreadNotifications(res.count);
      return res;
    },
    refetchInterval: 30_000,
  });

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
    queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
  }, [queryClient]);

  const markAll = useMutation({
    mutationFn: () => markAllRead(),
    onMutate: () => setUnreadNotifications(0),
    onSuccess: invalidate,
  });

  const clearAll = useMutation({
    mutationFn: () => clearAllNotifications(),
    onMutate: () => setUnreadNotifications(0),
    onSuccess: invalidate,
  });

  const dismiss = useMutation({
    mutationFn: dismissNotification,
    onMutate: () => decrementNotifications(1),
    onSuccess: invalidate,
  });

  // Mark visible items read whenever the panel opens (badge -> 0).
  const markAllMutate = markAll.mutate;
  useEffect(() => {
    if (open) markAllMutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const close = useCallback(() => setOpen(false), []);

  const notifications: AppNotification[] = useMemo(
    () =>
      (data?.notifications ?? []).filter(
        (n) =>
          n.status !== "dismissed" &&
          (filter === "all" || CATEGORY[n.type] === filter),
      ),
    [data, filter],
  );

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="notification-backdrop"
            className="fixed inset-0 bg-black/30 z-40"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={close}
          />

          <motion.aside
            key="notification-panel"
            className="fixed right-0 top-0 bottom-0 w-[360px] bg-surface-raised border-l border-border z-50 flex flex-col"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", damping: 25, stiffness: 200 }}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-border">
              <h2 className="text-lg font-semibold">Notifications</h2>
              <button
                onClick={close}
                className="p-1 rounded hover:bg-surface text-muted hover:text-foreground transition-colors"
                aria-label="Close notifications"
              >
                <CloseIcon />
              </button>
            </div>

            {/* Bulk actions */}
            <div className="flex items-center gap-2 px-4 py-2 border-b border-border">
              <button
                onClick={() => markAll.mutate()}
                className="text-xs px-2 py-1 rounded hover:bg-surface text-muted hover:text-foreground transition-colors"
              >
                Mark all read
              </button>
              <button
                onClick={() => clearAll.mutate()}
                className="text-xs px-2 py-1 rounded hover:bg-surface text-muted hover:text-foreground transition-colors"
              >
                Clear all
              </button>
            </div>

            {/* Filter chips */}
            <div className="flex flex-wrap gap-1.5 px-4 py-2 border-b border-border">
              {CHIPS.map((chip) => (
                <button
                  key={chip.id}
                  onClick={() => setFilter(chip.id)}
                  aria-pressed={filter === chip.id}
                  className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                    filter === chip.id
                      ? "bg-accent/20 text-accent border-accent/40"
                      : "border-border text-muted hover:text-foreground"
                  }`}
                >
                  {chip.label}
                </button>
              ))}
            </div>

            {/* Scrollable list */}
            <div className="flex-1 overflow-y-auto">
              {notifications.length === 0 ? (
                <p className="text-muted text-sm text-center py-8">
                  You&apos;re all caught up.
                </p>
              ) : (
                <ul className="divide-y divide-border">
                  {notifications.map((n) => (
                    <li key={n.id} className="px-4 py-3 bg-surface">
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-medium truncate">
                            {n.title}
                          </p>
                          {n.body && (
                            <p className="text-xs text-muted mt-0.5 line-clamp-2">
                              {n.body}
                            </p>
                          )}
                          <p className="text-xs text-muted mt-1">
                            {formatDistanceToNow(
                              new Date(n.created_at * 1000),
                              {
                                addSuffix: true,
                              },
                            )}
                          </p>
                        </div>
                        <button
                          onClick={() => dismiss.mutate(n.id)}
                          className="shrink-0 p-1 rounded hover:bg-surface-raised text-muted hover:text-foreground transition-colors"
                          aria-label="Dismiss notification"
                        >
                          <CloseIcon />
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ui && npx vitest run src/components/notifications/__tests__/NotificationPanel.test.tsx && npx tsc --noEmit
```

Expected: **4 passed** and no type errors.

- [ ] **Step 5: Commit**

```
git add ui/src/components/notifications/NotificationPanel.tsx ui/src/stores/appStore.ts ui/src/components/notifications/__tests__/NotificationPanel.test.tsx
git commit -m "feat(ui): notification inbox with bulk actions, filter chips and single-source badge"
```

---

### Task 17: Settings — Notification rules section (per-type toggles, quiet hours, macOS sound, rate cap)

**Files:**

- Create: `ui/src/components/settings/NotificationsSection.tsx` (self-contained, auto-saving section mirroring `AutoArmSection.tsx` — the repo's established, testable Settings-section pattern)
- Modify: `ui/src/components/settings/Settings.tsx:26` (add import) and `:2009` (render the section directly after the existing inline Notifications `<Section>`, which stays as-is for the channel/webhook/email controls)
- Create (test): `ui/src/components/settings/__tests__/NotificationsSection.test.tsx`

**Interfaces:**

- Consumes (from Task 15): `NotificationsConfig` extended type; `getConfig`, `updateConfig` from `../../lib/api`. Binds to the new config fields (`macos_sound`, `muted_types`, `quiet_hours_enabled`, `quiet_start`, `quiet_end`, `max_per_hour`) that the Config cluster adds to the Python `NotificationsConfig` dataclass + `ConfigUpdateBody`.
- Produces: `NotificationsSection` component saving `PUT /api/config` `{notifications: {...}}` partials.

- [ ] **Step 1: Write the failing test** — `ui/src/components/settings/__tests__/NotificationsSection.test.tsx`

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { NotificationsSection } from "../NotificationsSection";
import { makeWrapper } from "../../../test/queryWrapper";

const CONFIG = {
  notifications: {
    enabled: true,
    in_app: true,
    macos: true,
    macos_sound: false,
    webhook: { enabled: false, url: "", format: "slack" },
    email: {
      enabled: false,
      smtp_host: "",
      smtp_port: 587,
      smtp_user: "",
      smtp_password: "",
      from_address: "",
      to_address: "",
      max_per_day: 20,
    },
    muted_types: [],
    macos_min_priority: "normal",
    external_min_priority: "high",
    max_per_hour: 12,
    per_type_max_per_hour: {},
    quiet_hours_enabled: true,
    quiet_start: "22:00",
    quiet_end: "08:00",
    task_digest: "daily",
    digest_time: "08:00",
    overdue_recheck_minutes: 360,
    dedup_window_minutes: 60,
    retention_days: 30,
    dismissed_retention_days: 7,
    max_rows: 500,
    default_reminder_before_due: "1d",
    overdue_check_interval: "6h",
  },
};

describe("NotificationsSection", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify(CONFIG), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("renders the macOS sound toggle off from config", async () => {
    render(<NotificationsSection id="notifications-rules" />, {
      wrapper: makeWrapper(),
    });
    await waitFor(() =>
      expect(
        screen.getByRole("switch", {
          name: "Play sound with macOS banners",
        }),
      ).toHaveAttribute("aria-checked", "false"),
    );
  });

  it("PUTs notifications.macos_sound=true when toggled on", async () => {
    render(<NotificationsSection id="notifications-rules" />, {
      wrapper: makeWrapper(),
    });
    const sw = await screen.findByRole("switch", {
      name: "Play sound with macOS banners",
    });
    fireEvent.click(sw);

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([, i]) => (i as RequestInit)?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(
      ([, i]) => (i as RequestInit)?.method === "PUT",
    )!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.notifications.macos_sound).toBe(true);
  });

  it("mutes an event type by toggling it off (adds to muted_types)", async () => {
    render(<NotificationsSection id="notifications-rules" />, {
      wrapper: makeWrapper(),
    });
    const sw = await screen.findByRole("switch", { name: "Insights" });
    expect(sw).toHaveAttribute("aria-checked", "true");

    fireEvent.click(sw);

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(
        ([, i]) => (i as RequestInit)?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const [, init] = fetchMock.mock.calls.find(
      ([, i]) => (i as RequestInit)?.method === "PUT",
    )!;
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.notifications.muted_types).toEqual(["insight"]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ui && npx vitest run src/components/settings/__tests__/NotificationsSection.test.tsx
```

Expected failure: `Cannot find module '../NotificationsSection'` — the component does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create `ui/src/components/settings/NotificationsSection.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getConfig, updateConfig } from "../../lib/api";
import { useToast } from "../common/Toast";
import type { NotificationsConfig } from "../../lib/types";

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-accent" : "bg-border"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-[2px]"
        }`}
      />
    </button>
  );
}

const NOTIFY_TYPES: { type: string; label: string }[] = [
  { type: "meeting_processed", label: "Meeting processed" },
  { type: "meeting_failed", label: "Meeting failed" },
  { type: "prep_ready", label: "Meeting prep ready" },
  { type: "insight", label: "Insights" },
  { type: "automation", label: "Automations" },
  { type: "task_overdue", label: "Task overdue" },
  { type: "task_reminder", label: "Task reminders" },
  { type: "digest", label: "Daily digest" },
];

/** Settings panel: per-type mute, quiet hours, macOS sound, and the hourly cap. */
export function NotificationsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });
  const n = config?.notifications;

  const save = useMutation({
    mutationFn: (patch: Partial<NotificationsConfig>) =>
      updateConfig({ notifications: patch }),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Notification settings saved.");
    },
    onError: () => toast.error("Failed to save notification settings."),
  });

  if (!n) return null;

  const muted = n.muted_types ?? [];
  const isMuted = (t: string) => muted.includes(t);
  const toggleType = (t: string, enabled: boolean) => {
    const next = enabled ? muted.filter((x) => x !== t) : [...muted, t];
    save.mutate({ muted_types: next });
  };

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Notification rules</legend>
      <h2 className="text-sm font-medium text-text-primary">
        Notification rules
      </h2>
      <p className="text-xs text-text-muted mt-1">
        Choose which events notify you, silence banners overnight, and cap how
        often you&apos;re interrupted.
      </p>

      <div className="divide-y divide-border mt-3">
        <div className="py-3 flex items-center justify-between">
          <span className="text-sm text-text-secondary">
            Play sound with macOS banners
          </span>
          <Toggle
            checked={n.macos_sound}
            onChange={(v) => save.mutate({ macos_sound: v })}
            label="Play sound with macOS banners"
          />
        </div>

        <div className="py-3 flex items-center justify-between">
          <span className="text-sm text-text-secondary">Quiet hours</span>
          <Toggle
            checked={n.quiet_hours_enabled}
            onChange={(v) => save.mutate({ quiet_hours_enabled: v })}
            label="Quiet hours"
          />
        </div>

        <div className="py-3 flex items-center justify-between gap-4">
          <span className="text-sm text-text-secondary">
            Quiet hours window
          </span>
          <div className="flex items-center gap-2">
            <input
              type="time"
              aria-label="Quiet hours start"
              value={n.quiet_start}
              onChange={(e) => save.mutate({ quiet_start: e.target.value })}
              className="rounded-md bg-surface border border-border px-2 py-1 text-sm"
            />
            <span className="text-xs text-text-muted">to</span>
            <input
              type="time"
              aria-label="Quiet hours end"
              value={n.quiet_end}
              onChange={(e) => save.mutate({ quiet_end: e.target.value })}
              className="rounded-md bg-surface border border-border px-2 py-1 text-sm"
            />
          </div>
        </div>

        <div className="py-3 flex items-center justify-between">
          <span className="text-sm text-text-secondary">
            Max notifications per hour
          </span>
          <input
            type="number"
            min={1}
            step={1}
            aria-label="Max notifications per hour"
            value={n.max_per_hour}
            onChange={(e) =>
              save.mutate({ max_per_hour: Number(e.target.value) })
            }
            className="w-24 text-right rounded-md bg-surface border border-border px-2 py-1 text-sm"
          />
        </div>

        <div className="py-3">
          <p className="text-xs font-medium text-text-secondary mb-2">
            Notify me about
          </p>
          <div className="flex flex-col gap-2">
            {NOTIFY_TYPES.map((t) => (
              <div key={t.type} className="flex items-center justify-between">
                <span className="text-sm text-text-secondary">{t.label}</span>
                <Toggle
                  checked={!isMuted(t.type)}
                  onChange={(v) => toggleType(t.type, v)}
                  label={t.label}
                />
              </div>
            ))}
          </div>
        </div>
      </div>
    </fieldset>
  );
}
```

Then wire it into `ui/src/components/settings/Settings.tsx`. Add the import next to the other section imports (after line 26 `import { AutoArmSection } from "./AutoArmSection";`):

```tsx
import { NotificationsSection } from "./NotificationsSection";
```

And render it immediately after the existing inline Notifications `<Section>` closes (line 2009), by replacing:

```tsx
          </Section>

          {/* Logging */}
```

with:

```tsx
          </Section>

          <NotificationsSection id="notifications-rules" />

          {/* Logging */}
```

(This block sits inside the `daemonRunning` branch, so `NotificationsSection`'s `getConfig` query only runs when the daemon is up — matching the surrounding extracted sections.)

- [ ] **Step 4: Run test to verify it passes**

```
cd ui && npx vitest run src/components/settings/__tests__/NotificationsSection.test.tsx && npx tsc --noEmit
```

Expected: **3 passed** and no type errors.

- [ ] **Step 5: Commit**

```
git add ui/src/components/settings/NotificationsSection.tsx ui/src/components/settings/Settings.tsx ui/src/components/settings/__tests__/NotificationsSection.test.tsx
git commit -m "feat(ui): notification rules settings (per-type mute, quiet hours, sound, hourly cap)"
```
