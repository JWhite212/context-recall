# Automations Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** User-defined rules that watch each processed meeting and, when flat all/any conditions match, run apply-tag / webhook / notify actions — evaluated in post-processing and reprocess-safe.

**Architecture:** `AutomationRepository` (rules CRUD + `automation_dispatches` dedupe) mirrors `TrackerRepository`. A **pure** `RuleEvaluator` matches a meeting-context dict against a rule. `ActionExecutor` reuses existing primitives (`repo.update_meeting`, `send_webhook`, `macos.send`). A `_run_automations` stage runs last in `_post_process_async`. New `automations` API router + a Settings `AutomationsSection` + fired-rule badges on the meeting.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, aiosqlite, FastAPI; React 19 + TS + Vitest.

## Global Constraints

- **British spelling** in new comments/logs.
- **Head `SCHEMA_VERSION` is 16** here → new tables at **v17**. Add a `if current_version < 17:` block _after_ the v16 block and **move the trailing `else:`** to the v17 block. Incremental blocks use a **literal** `PRAGMA user_version = 17`. Also add the two `executescript`s to the fresh-install (`< 1`) block.
- **Distinct naming** — module `automations`, routes `/api/automation-rules*` + `GET /api/meetings/{id}/automations`. Do not touch the existing `meeting_insights` / `insights` / `trackers` routers.
- **Reprocess model:** `apply_tag` is idempotent (always runs); `webhook`/`notify` are gated by `automation_dispatches` (fire once per (rule, meeting)). A dispatch is recorded for **every** matched rule, and side-effects run only when the rule was **not already dispatched** — so the dispatch table doubles as the "which rules fired" record.
- **Snapshot semantics:** all rules evaluate against **one** meeting-context snapshot taken at the start of the run (match all rules first, then execute), so an `apply_tag` in one rule cannot cascade into another rule's match.
- **LLM/HTTP off the loop:** webhook uses async httpx (`send_webhook` is already async); no blocking calls on the API loop.
- Lint `ruff check src/ tests/`; UI `cd ui && npx tsc --noEmit`. Commits end with the `Claude-Session:` trailer. Run `git commit` **standalone** (a compound `git commit && …` trips a false-positive hook block).

---

### Task 1: DB v17 — automation tables + migration

**Files:**

- Modify: `src/db/database.py`
- Test: `tests/test_db_migration_v17.py` (create)

**Interfaces (Produces):** tables `automation_rules(id,name,enabled,match_mode,conditions_json,actions_json,created_at,updated_at)` and `automation_dispatches(rule_id,meeting_id,created_at)`.

- [ ] **Step 1: Write the failing test** — `tests/test_db_migration_v17.py`:

```python
import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v17_creates_automation_tables(tmp_path):
    db = Database(db_path=tmp_path / "v17.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 17
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('automation_rules','automation_dispatches') ORDER BY name"
        )
        assert [r[0] for r in await cur.fetchall()] == [
            "automation_dispatches",
            "automation_rules",
        ]
    finally:
        await db.close()


async def test_v17_upgrade_from_v16_preserves_data(tmp_path):
    db_path = tmp_path / "v16old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, started_at REAL)")
        await conn.execute("INSERT INTO meetings (id, started_at) VALUES ('m1', 1.0)")
        await conn.execute("PRAGMA user_version = 16")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='automation_rules'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("SELECT id FROM meetings WHERE id='m1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db_migration_v17.py -v`
Expected: FAIL (`SCHEMA_VERSION` is 16; tables absent).

- [ ] **Step 3: Write minimal implementation** in `src/db/database.py`:

1. `SCHEMA_VERSION = 16` → `17`.
2. Add the table SQL near `INSIGHT_RESULTS_SQL`:

```python
AUTOMATION_RULES_SQL = """
CREATE TABLE IF NOT EXISTS automation_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    match_mode TEXT NOT NULL DEFAULT 'all',
    conditions_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

AUTOMATION_DISPATCHES_SQL = """
CREATE TABLE IF NOT EXISTS automation_dispatches (
    rule_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (rule_id, meeting_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_automation_dispatches_meeting
    ON automation_dispatches(meeting_id);
"""
```

3. In the fresh-install block, before its `PRAGMA user_version = {SCHEMA_VERSION}` line, add:

```python
            await self.conn.executescript(AUTOMATION_RULES_SQL)
            await self.conn.executescript(AUTOMATION_DISPATCHES_SQL)
```

4. Replace the v16 block's trailing `else:` with a new v17 block. Current tail:

```python
            logger.info("Database migrated to version 16 (custom insights)")
            current_version = 16
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

→

```python
            logger.info("Database migrated to version 16 (custom insights)")
            current_version = 16

        if current_version < 17:
            # Automations: user-defined condition→action rules.
            await self.conn.executescript(AUTOMATION_RULES_SQL)
            await self.conn.executescript(AUTOMATION_DISPATCHES_SQL)
            await self.conn.execute("PRAGMA user_version = 17")
            await self.conn.commit()
            logger.info("Database migrated to version 17 (automations)")
            current_version = 17
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Run test to verify it passes** (+ v15/v16 still green)

Run: `.venv/bin/python -m pytest tests/test_db_migration_v17.py tests/test_db_migration_v16.py tests/test_db_migration_v15.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/db/database.py tests/test_db_migration_v17.py
git commit -m "feat(db): automation_rules + automation_dispatches tables (v17)"
```

---

### Task 2: `AutomationRepository`

**Files:**

- Create: `src/automations/__init__.py`, `src/automations/repository.py`
- Test: `tests/test_automations_repository.py`

**Interfaces (Produces):** `AutomationRepository(db)` with `create(name, match_mode="all", conditions=None, actions=None, enabled=True) -> str`, `get(id) -> dict|None`, `update(id, *, name=None, match_mode=None, conditions=None, actions=None, enabled=None)`, `list_rules(enabled_only=False) -> list[dict]`, `delete(id) -> bool`, `has_dispatched(rule_id, meeting_id) -> bool`, `record_dispatch(rule_id, meeting_id) -> None`, `fired_rules_for_meeting(meeting_id) -> list[dict]` (each `{id, name}`). A rule dict is `{id, name, enabled(bool), match_mode, conditions(list), actions(list), created_at, updated_at}`.

- [ ] **Step 1: Write the failing test** — `tests/test_automations_repository.py`:

```python
import pytest

from src.automations.repository import AutomationRepository


@pytest.fixture
async def auto_repo(db):
    return AutomationRepository(db)


@pytest.mark.asyncio
async def test_rule_crud(auto_repo):
    rid = await auto_repo.create(
        name="Tag discovery",
        match_mode="any",
        conditions=[{"field": "tag", "value": "Type/Discovery"}],
        actions=[{"type": "apply_tag", "tags": ["Reviewed"]}],
    )
    r = await auto_repo.get(rid)
    assert r["name"] == "Tag discovery"
    assert r["match_mode"] == "any"
    assert r["enabled"] is True
    assert r["conditions"] == [{"field": "tag", "value": "Type/Discovery"}]
    assert r["actions"] == [{"type": "apply_tag", "tags": ["Reviewed"]}]
    await auto_repo.update(rid, enabled=False, name="Tag discovery mtgs")
    r = await auto_repo.get(rid)
    assert r["enabled"] is False
    assert r["name"] == "Tag discovery mtgs"
    assert await auto_repo.list_rules(enabled_only=True) == []
    assert len(await auto_repo.list_rules()) == 1
    assert await auto_repo.delete(rid) is True
    assert await auto_repo.get(rid) is None


@pytest.mark.asyncio
async def test_dispatch_dedupe(auto_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    rid = await auto_repo.create(name="R", conditions=[{"field": "tag", "value": "x"}], actions=[])
    assert await auto_repo.has_dispatched(rid, mid) is False
    await auto_repo.record_dispatch(rid, mid)
    assert await auto_repo.has_dispatched(rid, mid) is True
    # Idempotent: a second record must not raise (INSERT OR IGNORE).
    await auto_repo.record_dispatch(rid, mid)
    fired = await auto_repo.fired_rules_for_meeting(mid)
    assert fired == [{"id": rid, "name": "R"}]


@pytest.mark.asyncio
async def test_fired_survives_rule_delete(auto_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    rid = await auto_repo.create(name="R", conditions=[{"field": "tag", "value": "x"}], actions=[])
    await auto_repo.record_dispatch(rid, mid)
    await auto_repo.delete(rid)
    # Dispatch row survives (no FK on rule_id); join drops the now-missing name.
    assert await auto_repo.fired_rules_for_meeting(mid) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_automations_repository.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation** — `src/automations/__init__.py` (empty file) and `src/automations/repository.py`:

```python
"""Data access for automation rules and their per-meeting dispatch records."""

import json
import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.automations")


class AutomationRepository:
    """Async CRUD for automation_rules + automation_dispatches."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        name: str,
        match_mode: str = "all",
        conditions: list | None = None,
        actions: list | None = None,
        enabled: bool = True,
    ) -> str:
        rule_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO automation_rules "
                "(id, name, enabled, match_mode, conditions_json, actions_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    name,
                    1 if enabled else 0,
                    match_mode,
                    json.dumps(conditions or []),
                    json.dumps(actions or []),
                    now,
                    now,
                ),
            )
            await self._db.conn.commit()
        return rule_id

    async def update(
        self,
        rule_id: str,
        *,
        name=None,
        match_mode=None,
        conditions=None,
        actions=None,
        enabled=None,
    ) -> None:
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if match_mode is not None:
            fields["match_mode"] = match_mode
        if conditions is not None:
            fields["conditions_json"] = json.dumps(conditions)
        if actions is not None:
            fields["actions_json"] = json.dumps(actions)
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        if not fields:
            return
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE automation_rules SET {set_clause} WHERE id = ?",
                [v for _, v in pairs] + [rule_id],
            )
            await self._db.conn.commit()

    async def get(self, rule_id: str) -> dict | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM automation_rules WHERE id = ?", (rule_id,)
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_rules(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        cur = await self._db.conn.execute(
            f"SELECT * FROM automation_rules {where} ORDER BY created_at"
        )
        return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def delete(self, rule_id: str) -> bool:
        async with self._db.write_lock:
            cur = await self._db.conn.execute(
                "DELETE FROM automation_rules WHERE id = ?", (rule_id,)
            )
            await self._db.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["conditions"] = json.loads(d.pop("conditions_json") or "[]")
        except (ValueError, TypeError):
            d["conditions"] = []
        try:
            d["actions"] = json.loads(d.pop("actions_json") or "[]")
        except (ValueError, TypeError):
            d["actions"] = []
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    # ------------------------------------------------------------------
    # Dispatches
    # ------------------------------------------------------------------

    async def has_dispatched(self, rule_id: str, meeting_id: str) -> bool:
        cur = await self._db.conn.execute(
            "SELECT 1 FROM automation_dispatches WHERE rule_id = ? AND meeting_id = ?",
            (rule_id, meeting_id),
        )
        return await cur.fetchone() is not None

    async def record_dispatch(self, rule_id: str, meeting_id: str) -> None:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT OR IGNORE INTO automation_dispatches "
                "(rule_id, meeting_id, created_at) VALUES (?, ?, ?)",
                (rule_id, meeting_id, time.time()),
            )
            await self._db.conn.commit()

    async def fired_rules_for_meeting(self, meeting_id: str) -> list[dict]:
        cur = await self._db.conn.execute(
            "SELECT r.id AS id, r.name AS name FROM automation_dispatches d "
            "JOIN automation_rules r ON r.id = d.rule_id "
            "WHERE d.meeting_id = ? ORDER BY d.created_at",
            (meeting_id,),
        )
        return [{"id": r["id"], "name": r["name"]} for r in await cur.fetchall()]
```

> Confirm `self._db.write_lock` / `self._db.conn` match `TrackerRepository` (they do). The `db`/`repo` test fixtures come from `tests/conftest.py`, same as `test_insights_repository.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_automations_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/automations/__init__.py src/automations/repository.py tests/test_automations_repository.py
git commit -m "feat(automations): AutomationRepository (rules CRUD + dispatch dedupe)"
```

---

### Task 3: `RuleEvaluator` (pure matcher)

**Files:**

- Create: `src/automations/evaluator.py`
- Test: `tests/test_automations_evaluator.py`

**Interfaces (Produces):** module functions `domains_from_attendees(attendees_json: str) -> list[str]`, `build_meeting_context(meeting) -> dict` (reads `meeting.tags`, `meeting.client_id`, `meeting.project_id`, `meeting.title`, `meeting.attendees_json`), and `matches(context: dict, rule: dict) -> bool`. Context shape: `{tags: list[str], client_id, project_id, title: str, attendee_domains: list[str]}`.

- [ ] **Step 1: Write the failing test** — `tests/test_automations_evaluator.py`:

```python
from types import SimpleNamespace

from src.automations.evaluator import (
    build_meeting_context,
    domains_from_attendees,
    matches,
)

_CTX = {
    "tags": ["Type/Discovery", "Client/Acme"],
    "client_id": "c1",
    "project_id": "p1",
    "title": "Acme Discovery call",
    "attendee_domains": ["acme.com"],
}


def _rule(conditions, match_mode="all"):
    return {"match_mode": match_mode, "conditions": conditions}


def test_tag_condition():
    assert matches(_CTX, _rule([{"field": "tag", "value": "Type/Discovery"}])) is True
    assert matches(_CTX, _rule([{"field": "tag", "value": "Type/Review"}])) is False


def test_client_project_title_domain():
    assert matches(_CTX, _rule([{"field": "client", "value": "c1"}])) is True
    assert matches(_CTX, _rule([{"field": "project", "value": "pX"}])) is False
    assert matches(_CTX, _rule([{"field": "title_contains", "value": "discovery"}])) is True
    assert matches(_CTX, _rule([{"field": "attendee_domain", "value": "ACME.com"}])) is True


def test_all_vs_any():
    conds = [
        {"field": "tag", "value": "Type/Discovery"},
        {"field": "tag", "value": "Type/Review"},
    ]
    assert matches(_CTX, _rule(conds, "all")) is False
    assert matches(_CTX, _rule(conds, "any")) is True


def test_empty_conditions_and_unknown_field():
    assert matches(_CTX, _rule([])) is False
    assert matches(_CTX, _rule([{"field": "nonsense", "value": "x"}])) is False


def test_domains_from_attendees_handles_dicts_and_strings():
    j = '[{"name": "A", "email": "a@Acme.com"}, "b@beta.io", {"name": "no email"}]'
    assert domains_from_attendees(j) == ["acme.com", "beta.io"]
    assert domains_from_attendees("") == []
    assert domains_from_attendees("not json") == []


def test_build_meeting_context():
    meeting = SimpleNamespace(
        tags=["T"],
        client_id="c1",
        project_id=None,
        title="Hi",
        attendees_json='[{"email": "x@y.com"}]',
    )
    ctx = build_meeting_context(meeting)
    assert ctx["tags"] == ["T"]
    assert ctx["client_id"] == "c1"
    assert ctx["attendee_domains"] == ["y.com"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_automations_evaluator.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation** — `src/automations/evaluator.py`:

```python
"""Pure matching of a meeting-context against automation rules.

No I/O, no DB, no LLM — every branch is unit-testable in isolation.
"""

import json


def domains_from_attendees(attendees_json: str) -> list[str]:
    """Extract lowercased email domains from a meeting's attendees JSON.

    Handles both the calendar shape (list of ``{"name","email"}`` dicts)
    and the plain-name/string shapes other paths store. Order-preserving,
    de-duplicated.
    """
    try:
        raw = json.loads(attendees_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    domains: list[str] = []
    for entry in raw:
        email = ""
        if isinstance(entry, dict):
            email = str(entry.get("email") or "")
        elif isinstance(entry, str):
            email = entry
        if "@" not in email:
            continue
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def build_meeting_context(meeting) -> dict:
    """Snapshot the fields automation conditions can match on."""
    return {
        "tags": list(getattr(meeting, "tags", None) or []),
        "client_id": getattr(meeting, "client_id", None),
        "project_id": getattr(meeting, "project_id", None),
        "title": getattr(meeting, "title", "") or "",
        "attendee_domains": domains_from_attendees(getattr(meeting, "attendees_json", "") or ""),
    }


def _condition_matches(context: dict, condition: dict) -> bool:
    field = condition.get("field")
    value = condition.get("value")
    if field == "tag":
        return value in context["tags"]
    if field == "client":
        return context["client_id"] == value
    if field == "project":
        return context["project_id"] == value
    if field == "title_contains":
        return bool(value) and str(value).lower() in context["title"].lower()
    if field == "attendee_domain":
        return str(value or "").strip().lower() in context["attendee_domains"]
    # Unknown field — never matches (forward-compatible / defensive).
    return False


def matches(context: dict, rule: dict) -> bool:
    conditions = rule.get("conditions") or []
    if not conditions:
        return False
    results = (_condition_matches(context, c) for c in conditions)
    if rule.get("match_mode") == "any":
        return any(results)
    return all(results)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_automations_evaluator.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/automations/evaluator.py tests/test_automations_evaluator.py
git commit -m "feat(automations): pure RuleEvaluator (flat all/any conditions)"
```

---

### Task 4: `ActionExecutor`

**Files:**

- Create: `src/automations/executor.py`
- Test: `tests/test_automations_executor.py`

**Interfaces (Consumes):** `MeetingRepository.update_meeting`, `src.notifications.channels.external.send_webhook`, `src.notifications.channels.macos.send`, `src.utils.config.WebhookChannelConfig`.
**Interfaces (Produces):** `ActionExecutor(repo, emit)` with `async run_rule(rule: dict, context: dict, meeting_id: str, *, run_side_effects: bool) -> None`. `emit` is a `(event_type, **kwargs) -> None` callable. `apply_tag` mutates `context["tags"]` in place (so multiple tag rules accumulate) and persists; `webhook`/`notify` run only when `run_side_effects`.

- [ ] **Step 1: Write the failing test** — `tests/test_automations_executor.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.automations.executor import ActionExecutor


def _ctx():
    return {"tags": ["Existing"], "client_id": None, "project_id": None, "title": "T", "attendee_domains": []}


def test_apply_tag_dedupes_and_persists():
    repo = MagicMock()
    repo.update_meeting = AsyncMock()
    ex = ActionExecutor(repo, emit=lambda *a, **k: None)
    ctx = _ctx()
    rule = {"name": "R", "actions": [{"type": "apply_tag", "tags": ["Existing", "New"]}]}
    asyncio.run(ex.run_rule(rule, ctx, "m1", run_side_effects=True))
    repo.update_meeting.assert_awaited_once_with("m1", tags=["Existing", "New"])
    assert ctx["tags"] == ["Existing", "New"]


def test_webhook_runs_only_with_side_effects():
    repo = MagicMock()
    repo.update_meeting = AsyncMock()
    rule = {"name": "R", "actions": [{"type": "webhook", "url": "https://h/x", "format": "generic"}]}
    with patch("src.automations.executor.send_webhook", new=AsyncMock(return_value=True)) as sw:
        ex = ActionExecutor(repo, emit=lambda *a, **k: None)
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=False))
        sw.assert_not_awaited()
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=True))
        sw.assert_awaited_once()
        cfg = sw.await_args.args[0]
        assert cfg.url == "https://h/x"
        assert cfg.format == "generic"


def test_notify_emits_and_banners():
    repo = MagicMock()
    events = []
    rule = {"name": "R", "actions": [{"type": "notify", "message": "heads up"}]}
    with patch("src.automations.executor.macos_send", new=AsyncMock()) as banner:
        ex = ActionExecutor(repo, emit=lambda et, **k: events.append((et, k)))
        asyncio.run(ex.run_rule(rule, _ctx(), "m1", run_side_effects=True))
        banner.assert_awaited_once()
        assert events and events[0][0] == "notification"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_automations_executor.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation** — `src/automations/executor.py`:

```python
"""Runs the actions of a matched automation rule, reusing existing primitives."""

import logging

from src.notifications.channels.external import send_webhook
from src.notifications.channels.macos import send as macos_send
from src.utils.config import WebhookChannelConfig

logger = logging.getLogger("contextrecall.automations.executor")


class ActionExecutor:
    """Executes apply_tag / webhook / notify actions for one meeting."""

    def __init__(self, repo, emit) -> None:
        self._repo = repo
        self._emit = emit

    async def run_rule(
        self, rule: dict, context: dict, meeting_id: str, *, run_side_effects: bool
    ) -> None:
        for action in rule.get("actions") or []:
            atype = action.get("type")
            try:
                if atype == "apply_tag":
                    await self._apply_tag(action, context, meeting_id)
                elif atype == "webhook" and run_side_effects:
                    await self._webhook(action, context, rule)
                elif atype == "notify" and run_side_effects:
                    await self._notify(action, context, rule, meeting_id)
            except Exception:
                logger.warning(
                    "Automation action %s failed for rule '%s'", atype, rule.get("name"),
                    exc_info=True,
                )

    async def _apply_tag(self, action: dict, context: dict, meeting_id: str) -> None:
        tags = list(context.get("tags") or [])
        changed = False
        for tag in action.get("tags") or []:
            if tag and tag not in tags:
                tags.append(tag)
                changed = True
        if not changed:
            return
        context["tags"] = tags  # accumulate for later rules in the same run
        await self._repo.update_meeting(meeting_id, tags=tags)

    async def _webhook(self, action: dict, context: dict, rule: dict) -> None:
        cfg = WebhookChannelConfig(
            enabled=True,
            url=action.get("url", ""),
            format=action.get("format", "generic"),
        )
        title = context.get("title") or "Context Recall"
        body = f"Automation '{rule.get('name')}' matched."
        await send_webhook(cfg, title, body, "automation")

    async def _notify(self, action: dict, context: dict, rule: dict, meeting_id: str) -> None:
        title = context.get("title") or "Context Recall"
        body = action.get("message") or f"Automation '{rule.get('name')}' matched."
        self._emit("notification", title=title, body=body, meeting_id=meeting_id)
        await macos_send(title, body)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_automations_executor.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src/automations/ tests/test_automations_executor.py
git add src/automations/executor.py tests/test_automations_executor.py
git commit -m "feat(automations): ActionExecutor (apply-tag/webhook/notify)"
```

---

### Task 5: Config `AutomationsConfig`

**Files:**

- Modify: `src/utils/config.py`, `config.example.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test** (add to `tests/test_config.py`):

```python
def test_automations_config_defaults():
    from src.utils.config import AutomationsConfig

    cfg = AutomationsConfig()
    assert cfg.enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -k automations_config -q`
Expected: FAIL (`AutomationsConfig` missing).

- [ ] **Step 3: Write minimal implementation** — add near `InsightsConfig`:

```python
@dataclass
class AutomationsConfig:
    enabled: bool = True
```

Wire into `AppConfig` (after `insights`): `automations: AutomationsConfig = field(default_factory=AutomationsConfig)`; and into `load_config`'s `AppConfig(...)` call (after `insights=...`): `automations=_build_dataclass(AutomationsConfig, raw.get("automations", {})),`. Add to `config.example.yaml`:

```yaml
# --- Automations ---
automations:
  # Run user-defined rules (Settings → Automations) after each meeting.
  # enabled: true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_config.py
git commit -m "feat(config): AutomationsConfig"
```

---

### Task 6: Pipeline `_run_automations`

**Files:**

- Modify: `src/pipeline_runner.py`
- Test: `tests/test_pipeline_runner.py`

**Interfaces (Consumes):** `AutomationRepository`, `RuleEvaluator.matches`, `build_meeting_context`, `ActionExecutor`, `self._config.automations`, `self._db.repo.get_meeting`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_pipeline_runner.py`):

```python
def test_post_processing_runs_automations(tmp_path, loop_thread):
    repo = _make_repo()
    repo.get_meeting = AsyncMock(
        return_value=MagicMock(
            tags=["Type/Discovery"], client_id=None, project_id=None,
            title="Disco", attendees_json="[]",
        )
    )
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    config.automations.enabled = True
    runner = _make_runner(config, db=bridge)

    auto_repo = MagicMock()
    auto_repo.list_rules = AsyncMock(
        return_value=[{
            "id": "r1", "name": "R", "enabled": True, "match_mode": "all",
            "conditions": [{"field": "tag", "value": "Type/Discovery"}],
            "actions": [{"type": "apply_tag", "tags": ["Reviewed"]}],
        }]
    )
    auto_repo.has_dispatched = AsyncMock(return_value=False)
    auto_repo.record_dispatch = AsyncMock()

    with (
        patch("src.automations.repository.AutomationRepository", return_value=auto_repo),
        patch("src.analytics.engine.AnalyticsEngine") as engine_cls,
    ):
        engine_cls.return_value.refresh_period = AsyncMock()
        asyncio.run(
            runner._post_process_async(
                "m1", _make_transcript(), started_at=1000.0, is_reprocess=False
            )
        )

    auto_repo.record_dispatch.assert_awaited_once_with("r1", "m1")
    repo.update_meeting.assert_any_await("m1", tags=["Type/Discovery", "Reviewed"])
```

Also add `config.automations.enabled = False` to `_make_config` (after `config.insights.enabled = False`) so other pipeline tests don't trigger automations.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_runner.py -k runs_automations -q`
Expected: FAIL (no `_run_automations`).

- [ ] **Step 3: Write minimal implementation** — in `_post_process_async`, add a guarded block **after** the insights block (last before analytics):

```python
        try:
            autos_cfg = getattr(self._config, "automations", None)
            if autos_cfg and autos_cfg.enabled:
                await self._run_automations(meeting_id)
        except Exception:
            logger.warning("Automations run failed", exc_info=True)
```

Add the method (mirrors `_scan_trackers`; snapshot-then-execute):

```python
    async def _run_automations(self, meeting_id: str) -> None:
        from src.automations.evaluator import build_meeting_context, matches
        from src.automations.executor import ActionExecutor
        from src.automations.repository import AutomationRepository

        if self._db.database is None:
            return
        auto_repo = AutomationRepository(self._db.database)
        rules = await auto_repo.list_rules(enabled_only=True)
        if not rules:
            return
        meeting = await self._db.repo.get_meeting(meeting_id)
        if meeting is None:
            return
        # One snapshot for the whole run — match all rules before executing so
        # an apply_tag cannot cascade into another rule's match.
        context = build_meeting_context(meeting)
        matched = [r for r in rules if matches(context, r)]
        if not matched:
            return
        executor = ActionExecutor(self._db.repo, self._emit)
        for rule in matched:
            already = await auto_repo.has_dispatched(rule["id"], meeting_id)
            await executor.run_rule(rule, context, meeting_id, run_side_effects=not already)
            await auto_repo.record_dispatch(rule["id"], meeting_id)
        self._emit(
            "automations.fired",
            meeting_id=meeting_id,
            rules=[{"id": r["id"], "name": r["name"]} for r in matched],
        )
```

- [ ] **Step 4: Run test to verify it passes** (+ full runner suite)

Run: `.venv/bin/python -m pytest tests/test_pipeline_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline_runner.py tests/test_pipeline_runner.py
git commit -m "feat(pipeline): run automations in post-processing"
```

---

### Task 7: API route + server registration

**Files:**

- Create: `src/api/routes/automations.py`
- Modify: `src/api/server.py`
- Test: `tests/test_api_automations.py`

**Interfaces (Produces):** `GET/POST /api/automation-rules`, `PATCH/DELETE /api/automation-rules/{id}`, `GET /api/meetings/{id}/automations`.

- [ ] **Step 1: Write the failing test** — `tests/test_api_automations.py` (clone `test_api_insights.py`):

```python
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import automations as auto_routes
from src.automations.repository import AutomationRepository
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-for-automations"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "auto_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    auto_repo = AutomationRepository(db)
    auto_routes.init(repo, auto_repo)
    app = FastAPI()
    app.include_router(auto_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "auto_repo": auto_repo}
    await db.close()


@pytest.mark.asyncio
async def test_rule_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/automation-rules",
            headers=_auth_headers(),
            json={
                "name": "R",
                "match_mode": "all",
                "conditions": [{"field": "tag", "value": "Type/Discovery"}],
                "actions": [{"type": "apply_tag", "tags": ["Reviewed"]}],
            },
        )
        assert created.status_code == 201
        rid = created.json()["id"]
        patched = c.patch(
            f"/api/automation-rules/{rid}", headers=_auth_headers(), json={"enabled": False}
        )
        assert patched.json()["enabled"] is False
        assert c.delete(f"/api/automation-rules/{rid}", headers=_auth_headers()).status_code == 200
        assert c.get("/api/automation-rules", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_create_rejects_empty_conditions(api):
    with TestClient(api["app"]) as c:
        r = c.post(
            "/api/automation-rules",
            headers=_auth_headers(),
            json={"name": "R", "conditions": [], "actions": [{"type": "notify"}]},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_meeting_automations_404_for_unknown_meeting(api):
    with TestClient(api["app"]) as c:
        assert c.get("/api/meetings/nope/automations", headers=_auth_headers()).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_automations.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation** — `src/api/routes/automations.py`:

```python
"""
Automation rule endpoints.

GET    /api/automation-rules          — list rules
POST   /api/automation-rules          — create
PATCH  /api/automation-rules/{id}     — update
DELETE /api/automation-rules/{id}     — delete (dispatch history preserved)
GET    /api/meetings/{id}/automations — rules that fired for a meeting
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("contextrecall.api.automations")

router = APIRouter()

_repo = None  # MeetingRepository
_auto_repo = None  # AutomationRepository


def init(repo, auto_repo) -> None:
    global _repo, _auto_repo
    _repo = repo
    _auto_repo = auto_repo


def _require_repos() -> None:
    if not _repo or not _auto_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class Condition(BaseModel):
    field: Literal["tag", "client", "project", "title_contains", "attendee_domain"]
    value: str = Field(min_length=1, max_length=500)


class Action(BaseModel):
    type: Literal["apply_tag", "webhook", "notify"]
    tags: list[str] | None = None
    url: str | None = None
    format: str | None = None
    message: str | None = None


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    match_mode: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(min_length=1)
    actions: list[Action] = Field(min_length=1)
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    match_mode: Literal["all", "any"] | None = None
    conditions: list[Condition] | None = Field(default=None, min_length=1)
    actions: list[Action] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


@router.get("/api/automation-rules")
async def list_rules():
    _require_repos()
    return await _auto_repo.list_rules()


@router.post("/api/automation-rules", status_code=201)
async def create_rule(body: RuleCreate):
    _require_repos()
    rule_id = await _auto_repo.create(
        name=body.name.strip(),
        match_mode=body.match_mode,
        conditions=[c.model_dump() for c in body.conditions],
        actions=[a.model_dump(exclude_none=True) for a in body.actions],
        enabled=body.enabled,
    )
    return await _auto_repo.get(rule_id)


@router.patch("/api/automation-rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    _require_repos()
    if not await _auto_repo.get(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    await _auto_repo.update(
        rule_id,
        name=body.name.strip() if body.name is not None else None,
        match_mode=body.match_mode,
        conditions=[c.model_dump() for c in body.conditions] if body.conditions is not None else None,
        actions=[a.model_dump(exclude_none=True) for a in body.actions] if body.actions is not None else None,
        enabled=body.enabled,
    )
    return await _auto_repo.get(rule_id)


@router.delete("/api/automation-rules/{rule_id}")
async def delete_rule(rule_id: str):
    _require_repos()
    if not await _auto_repo.delete(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": rule_id}


@router.get("/api/meetings/{meeting_id}/automations")
async def meeting_automations(meeting_id: str):
    _require_repos()
    if not await _repo.get_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    return await _auto_repo.fired_rules_for_meeting(meeting_id)
```

Register in `src/api/server.py` mirroring insights (read the exact lines first):

- with the other route imports: `from src.api.routes import automations as automations_routes`
- in the wiring block (near `insights_routes.init`): `from src.automations.repository import AutomationRepository` + `automations_routes.init(self.repo, AutomationRepository(self.db))`
- after `auth_deps` is defined (near `include_router(insights_routes...)`): `app.include_router(automations_routes.router, dependencies=auth_deps)`

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_automations.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/bin/ruff check src/api/routes/automations.py
git add src/api/routes/automations.py src/api/server.py tests/test_api_automations.py
git commit -m "feat(api): automation rule CRUD + meeting fired-rules"
```

---

### Task 8: UI types + API client

**Files:**

- Modify: `ui/src/lib/types.ts`, `ui/src/lib/api.ts`
- Test: `ui/src/lib/__tests__/api.test.ts`

- [ ] **Step 1: Write the failing test** (add to `api.test.ts`, matching its fetch-spy style):

```ts
it("createAutomationRule POSTs the body", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: input.toString(), init });
      return new Response(JSON.stringify({ id: "r1" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    },
  ) as unknown as typeof fetch;
  await createAutomationRule({
    name: "R",
    match_mode: "all",
    conditions: [{ field: "tag", value: "Type/Discovery" }],
    actions: [{ type: "apply_tag", tags: ["Reviewed"] }],
  });
  const call = calls.find((c) => c.init?.method === "POST");
  expect(call?.url).toContain("/api/automation-rules");
  expect(JSON.parse(call?.init?.body as string).name).toBe("R");
});
```

Add `createAutomationRule` to the test's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`
Expected: FAIL (`createAutomationRule` undefined).

- [ ] **Step 3: Write minimal implementation** — in `types.ts` add:

```ts
export type AutomationConditionField =
  "tag" | "client" | "project" | "title_contains" | "attendee_domain";

export interface AutomationCondition {
  field: AutomationConditionField;
  value: string;
}

export type AutomationActionType = "apply_tag" | "webhook" | "notify";

export interface AutomationAction {
  type: AutomationActionType;
  tags?: string[];
  url?: string;
  format?: string;
  message?: string;
}

export interface AutomationRule {
  id: string;
  name: string;
  enabled: boolean;
  match_mode: "all" | "any";
  conditions: AutomationCondition[];
  actions: AutomationAction[];
  created_at: number;
  updated_at: number;
}

export interface MeetingAutomation {
  id: string;
  name: string;
}
```

In `api.ts` (add the new types to the type-import block):

```ts
export async function getAutomationRules(): Promise<AutomationRule[]> {
  return request<AutomationRule[]>("/api/automation-rules");
}

export async function createAutomationRule(rule: {
  name: string;
  match_mode: "all" | "any";
  conditions: AutomationCondition[];
  actions: AutomationAction[];
  enabled?: boolean;
}): Promise<AutomationRule> {
  return request<AutomationRule>("/api/automation-rules", {
    method: "POST",
    body: JSON.stringify(rule),
  });
}

export async function updateAutomationRule(
  id: string,
  fields: Partial<
    Pick<
      AutomationRule,
      "name" | "match_mode" | "conditions" | "actions" | "enabled"
    >
  >,
): Promise<AutomationRule> {
  return request<AutomationRule>(
    `/api/automation-rules/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify(fields) },
  );
}

export async function deleteAutomationRule(id: string): Promise<void> {
  await request(`/api/automation-rules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function getMeetingAutomations(
  meetingId: string,
): Promise<MeetingAutomation[]> {
  return request<MeetingAutomation[]>(
    `/api/meetings/${encodeURIComponent(meetingId)}/automations`,
  );
}
```

- [ ] **Step 4: Run test to verify it passes** (+ tsc)

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts && npx tsc --noEmit`
Expected: PASS + no type errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/types.ts ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): automation-rule API client + types"
```

---

### Task 9: Settings — Automations management panel

**Files:**

- Create: `ui/src/components/settings/AutomationsSection.tsx`
- Modify: `ui/src/components/settings/Settings.tsx`
- Test: `ui/src/components/settings/__tests__/AutomationsSection.test.tsx` (create)

**Interfaces (Consumes):** `getAutomationRules`, `createAutomationRule`, `updateAutomationRule`, `deleteAutomationRule` (Task 8); the `Section`/`Field`/`Toggle`/`FORM_INPUT` primitives + `SETTINGS_SECTIONS` in `Settings.tsx`.

- [ ] **Step 1:** Read `ui/src/components/settings/InsightsSection.tsx` (the F2 panel) and, in `Settings.tsx`, the `SETTINGS_SECTIONS` array + the `Section`/`Field`/`Toggle`/`FORM_INPUT` primitives + the `InsightsSection` render site. `AutomationsSection` is its own self-contained file (importing those primitives from `Settings.tsx` or a shared module exactly as `InsightsSection.tsx` does). It mirrors `InsightsSection` but each rule has: **name** (text), **enabled** (`Toggle`), **match_mode** (`all`/`any` select), a **conditions** list (rows: `field` select over the 5 fields + a value text input) and an **actions** list (rows: `type` select over `apply_tag`/`webhook`/`notify` + the relevant value input — tags(comma) / url / message). Store via the api client, invalidating `["automation-rules"]`.

- [ ] **Step 2: Write the failing test** — `ui/src/components/settings/__tests__/AutomationsSection.test.tsx`: wrap in `QueryClientProvider` (retry:false) + `ToastProvider`; stub `fetch` to return `[{id:"r1",name:"Tag discovery",enabled:true,match_mode:"all",conditions:[{field:"tag",value:"Type/Discovery"}],actions:[{type:"apply_tag",tags:["Reviewed"]}],created_at:0,updated_at:0}]` for `GET /api/automation-rules`; render `<AutomationsSection id="automations" />`; `await waitFor` the rule name "Tag discovery" to appear. (Follow the `InsightsSection.test.tsx` wrapper pattern; assert a stable rendered value, not a brittle interaction.)

```tsx
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "../../common/Toast";
import { AutomationsSection } from "../AutomationsSection";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  );
}

describe("AutomationsSection", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      if (input.toString().includes("/api/automation-rules")) {
        return new Response(
          JSON.stringify([
            {
              id: "r1",
              name: "Tag discovery",
              enabled: true,
              match_mode: "all",
              conditions: [{ field: "tag", value: "Type/Discovery" }],
              actions: [{ type: "apply_tag", tags: ["Reviewed"] }],
              created_at: 0,
              updated_at: 0,
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
  });

  it("renders existing rules", async () => {
    wrap(<AutomationsSection id="automations" />);
    await waitFor(() =>
      expect(screen.getByText("Tag discovery")).toBeInTheDocument(),
    );
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ui && npx vitest run src/components/settings/__tests__/AutomationsSection.test.tsx`
Expected: FAIL (component missing).

- [ ] **Step 4: Write minimal implementation** — create `AutomationsSection.tsx` (mirror `InsightsSection.tsx`, reusing `Section`/`Field`/`Toggle`/`FORM_INPUT`) with the name + enabled + match_mode + conditions-builder + actions-builder CRUD described in Step 1; add `{ id: "automations", label: "Automations" }` to `SETTINGS_SECTIONS`; render `{daemonRunning && <AutomationsSection id="automations" />}` near the `InsightsSection` render site; import `AutomationsSection` in `Settings.tsx`.

- [ ] **Step 5: Run test to verify it passes** (+ tsc + full UI suite)

Run: `cd ui && npx vitest run src/components/settings/__tests__/AutomationsSection.test.tsx && npx tsc --noEmit && npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/settings/
git commit -m "feat(ui): Settings panel to manage automation rules"
```

---

### Task 10: Meeting fired-automations display

**Files:**

- Modify: `ui/src/components/meetings/MeetingInsights.tsx`
- Test: `ui/src/components/meetings/__tests__/AutomationBadges.test.tsx` (create)

**Interfaces (Consumes):** `getMeetingAutomations` (Task 8), `MeetingAutomation` type.

- [ ] **Step 1:** Read `MeetingInsights.tsx` (the F2 `InsightResults` export + the `useQuery` wiring + early-return gate). Add a pure `AutomationBadges({ names })` component exported from `MeetingInsights.tsx` (like `InsightResults`) that renders each fired rule name as a pill and returns null when empty; add a `useQuery(["meeting-automations", meetingId], () => getMeetingAutomations(meetingId))`; render `<AutomationBadges names={firedNames} />`; include the fired count in the early-return gate.

- [ ] **Step 2: Write the failing test** — `ui/src/components/meetings/__tests__/AutomationBadges.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AutomationBadges } from "../MeetingInsights";

describe("AutomationBadges", () => {
  it("renders a pill per fired rule", () => {
    render(<AutomationBadges names={["Tag discovery", "Notify me"]} />);
    expect(screen.getByText("Tag discovery")).toBeInTheDocument();
    expect(screen.getByText("Notify me")).toBeInTheDocument();
  });

  it("renders nothing when empty", () => {
    const { container } = render(<AutomationBadges names={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/AutomationBadges.test.tsx`
Expected: FAIL (`AutomationBadges` undefined).

- [ ] **Step 4: Write minimal implementation** — in `MeetingInsights.tsx`:

```tsx
/** Pills naming the automation rules that fired on a meeting. */
export function AutomationBadges({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {names.map((name, i) => (
        <span
          key={`${name}-${i}`}
          className="text-xs px-2 py-0.5 rounded-full bg-purple-400/20 text-purple-400"
          title="Automation rule that fired for this meeting"
        >
          {name}
        </span>
      ))}
    </div>
  );
}
```

Add the query + wiring inside `MeetingInsights` (import `getMeetingAutomations`):

```tsx
const { data: firedAutomations = [] } = useQuery({
  queryKey: ["meeting-automations", meetingId],
  queryFn: () => getMeetingAutomations(meetingId),
});
```

Add `firedAutomations.length === 0` to the early-return gate's `&&` chain, and render `<AutomationBadges names={firedAutomations.map((a) => a.name)} />` next to `<InsightResults … />`.

- [ ] **Step 5: Run test to verify it passes** (+ tsc + full UI suite)

Run: `cd ui && npx vitest run src/components/meetings/__tests__/AutomationBadges.test.tsx && npx tsc --noEmit && npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/meetings/
git commit -m "feat(ui): show fired automation rules on the meeting"
```

---

### Task 11: Feature verification

- [ ] `.venv/bin/python -m pytest tests/ -q` → all pass.
- [ ] `.venv/bin/ruff check src/ tests/` → clean.
- [ ] `cd ui && npm test && npx tsc --noEmit` → pass.
- [ ] `coderabbit review --agent --base origin/main` → address Critical/Warning, re-run until clean (run `git commit` standalone for any fixes; TDD each fix).
- [ ] Report per-task test counts + anything not done.

---

## Self-Review

**Spec coverage:** v17 tables/migration (T1) ✔; `AutomationRepository` CRUD + dispatch dedupe + `fired_rules_for_meeting` (T2) ✔; pure `RuleEvaluator` flat all/any over the 5 fields + domain derivation (T3) ✔; `ActionExecutor` apply-tag(idempotent)/webhook/notify reusing existing primitives (T4) ✔; config gate (T5) ✔; `_run_automations` snapshot-then-execute pipeline stage, reprocess-safe (T6) ✔; API CRUD + fired-rules, distinct routes (T7) ✔; UI types/client (T8), Settings panel (T9), meeting fired display (T10) ✔; verification (T11) ✔. Reprocess-safety = idempotent tags (T4) + `has_dispatched` gate (T6) + dispatch survives rule delete (T2 test).

**Placeholder scan:** new files (repository, evaluator, executor, route, config, migration, api fns, `AutomationBadges`) are complete code. Two steps say "read the exact region first" (server.py registration, Settings `InsightsSection`) — each names the file + what to match, actionable not vague. UI Tasks 9–10 mirror the F2 decision (own file + pure unit) to avoid brittle full renders.

**Type consistency:** rule dict `{id,name,enabled,match_mode,conditions,actions,created_at,updated_at}`; condition `{field,value}`; action `{type,tags?,url?,format?,message?}`; context `{tags,client_id,project_id,title,attendee_domains}`; fired rule `{id,name}` — identical across repository (T2), evaluator (T3), executor (T4), pipeline (T6), API (T7), and UI types (T8). Method names `list_rules`/`has_dispatched`/`record_dispatch`/`fired_rules_for_meeting`/`build_meeting_context`/`matches`/`run_rule` consistent across tasks. Routes `/api/automation-rules*` + `/api/meetings/{id}/automations` consistent between T7 and T8.
