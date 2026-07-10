# Auto-Prep Briefings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduler-driven sweep that pre-generates prep briefings for upcoming context-rich calendar events, wires the orphaned `PrepBriefingGenerator`, links briefings to events, and surfaces them in the Prep view + a read-only "prep ready" calendar badge.

**Architecture:** A new `PrepSweep` (pure qualification helpers + async orchestration) runs on the scheduler, reads upcoming events from the foundation's `calendar_events`, filters to context-rich ones (attendee-history or series-title match), and calls the now-wired `PrepBriefingGenerator` (LLM offloaded to a thread) to generate + link a briefing per event. New API + UI surface the briefings.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pytest + pytest-asyncio; React 19 + TypeScript, TanStack Query, react-markdown, Vitest 4.

## Global Constraints

- **Branch:** work on `feat/calendar-auto-prep` (off `feat/calendar-import`) — do NOT branch off `main`; this depends on the unmerged foundation.
- **Migration head is `SCHEMA_VERSION = 18`** → this feature bumps it to **19**. Column adds go in TWO places in `src/db/database.py`: the fresh-install `if current_version < 1:` block AND a new `if current_version < 19:` block; move the trailing `else: logger.debug(...)` after the new block. `prep_briefings` is **already** in `_ALLOWED_TABLES`, so `_safe_add_column(conn, "prep_briefings", ...)` needs no change there.
- **`PrepConfig.auto_generate` already exists** (default `True`) — do NOT re-add it. Add only `lookahead_hours`, `sweep_interval_minutes`, `max_per_sweep`.
- **LLM offload:** `PrepBriefingGenerator.generate()` must call the public `Summariser.chat(system, user)` seam via `await asyncio.get_running_loop().run_in_executor(None, ...)` — never block the event loop. `briefing.py` currently imports only `json` + `logging`; add `import asyncio`.
- **Prep router uses `prefix="/api/prep"`** and declares `/{meeting_id}` — new literal routes (`/upcoming-list`, `/prepared-events`) MUST be declared BEFORE the `/{meeting_id}` route or they'll be captured as a meeting id.
- **Repository style:** `PrepRepository` writes use `self._db.conn.execute(...)` + `await self._db.conn.commit()` with NO `write_lock` (match the existing `create`); reads use `self._db.conn.execute` then `fetchone`/`fetchall`; rows → `dict(row)`.
- **Scheduler jobs:** `self._scheduler.register("name", lambda: safe_run("name", self._method, timeout=T), interval_seconds)`; the prep sweep uses `timeout=300` (LLM is slow) and interval `config.prep.sweep_interval_minutes * 60`.
- **Tests:** migration tests are plain `async def test_...(tmp_path)` (no decorator); repository/API/sweep/server tests use `@pytest.mark.asyncio` + the shared `db` fixture. LLM/models are always stubbed — no real model loads.
- **Commands:** Python `python3 -m pytest <path> -v`, `ruff check src/ tests/`. UI `cd ui && npm test`, `cd ui && npx tsc --noEmit`. Use `source .venv/bin/activate` if `.venv` exists.

---

### Task 1: Migration v19 — prep_briefings event columns

**Files:**

- Modify: `src/db/database.py` (SCHEMA_VERSION, add columns + index in two blocks)
- Test: `tests/test_db_migration_v19.py`

**Interfaces:**

- Produces: `prep_briefings` gains `calendar_event_uid TEXT` + `event_signature TEXT` and index `idx_prep_briefings_cal_event`; `SCHEMA_VERSION == 19`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration_v19.py`:

```python
import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v19_adds_prep_event_columns(tmp_path):
    db = Database(db_path=tmp_path / "v19.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 19
        cur = await db.conn.execute("PRAGMA table_info(prep_briefings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "calendar_event_uid" in cols
        assert "event_signature" in cols
    finally:
        await db.close()


async def test_v19_upgrade_from_v18_preserves_data(tmp_path):
    db_path = tmp_path / "v18old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE prep_briefings (id TEXT PRIMARY KEY, meeting_id TEXT, "
            "content_markdown TEXT, generated_at REAL, expires_at REAL)"
        )
        await conn.execute(
            "INSERT INTO prep_briefings (id, content_markdown, generated_at, expires_at) "
            "VALUES ('p1', 'hi', 1.0, 9999999999.0)"
        )
        await conn.execute("PRAGMA user_version = 18")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute("PRAGMA table_info(prep_briefings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"calendar_event_uid", "event_signature"} <= cols
        cur = await db.conn.execute("SELECT id FROM prep_briefings WHERE id='p1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v19.py -v`
Expected: FAIL — `SCHEMA_VERSION` is 18 and the columns don't exist.

- [ ] **Step 3: Write minimal implementation**

In `src/db/database.py`:

(a) Bump: `SCHEMA_VERSION = 19`.

(b) In the fresh-install `if current_version < 1:` block, after the v18 `calendar_events` line and before `await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")`:

```python
            # Auto-prep event links (v19).
            await _safe_add_column(self.conn, "prep_briefings", "calendar_event_uid", "TEXT", "NULL")
            await _safe_add_column(self.conn, "prep_briefings", "event_signature", "TEXT", "NULL")
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prep_briefings_cal_event "
                "ON prep_briefings(calendar_event_uid)"
            )
```

(c) Replace the trailing block. Find:

```python
        if current_version < 18:
            # Calendar import: mirrored upcoming calendar events.
            await self.conn.executescript(CALENDAR_EVENTS_SQL)
            await self.conn.execute("PRAGMA user_version = 18")
            await self.conn.commit()
            logger.info("Database migrated to version 18 (calendar import)")
            current_version = 18
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

and change to:

```python
        if current_version < 18:
            # Calendar import: mirrored upcoming calendar events.
            await self.conn.executescript(CALENDAR_EVENTS_SQL)
            await self.conn.execute("PRAGMA user_version = 18")
            await self.conn.commit()
            logger.info("Database migrated to version 18 (calendar import)")
            current_version = 18

        if current_version < 19:
            # Auto-prep: link briefings to upcoming calendar events.
            await _safe_add_column(self.conn, "prep_briefings", "calendar_event_uid", "TEXT", "NULL")
            await _safe_add_column(self.conn, "prep_briefings", "event_signature", "TEXT", "NULL")
            await self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_prep_briefings_cal_event "
                "ON prep_briefings(calendar_event_uid)"
            )
            await self.conn.execute("PRAGMA user_version = 19")
            await self.conn.commit()
            logger.info("Database migrated to version 19 (auto-prep event links)")
            current_version = 19
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db_migration_v19.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/db/database.py tests/test_db_migration_v19.py
git commit -m "feat(db): prep_briefings event-link columns (v19)"
```

---

### Task 2: `PrepConfig` sweep fields

**Files:**

- Modify: `src/utils/config.py` (`PrepConfig`)
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**

- Produces: `PrepConfig` gains `lookahead_hours: int = 24`, `sweep_interval_minutes: int = 15`, `max_per_sweep: int = 5` (keeps existing `lead_time_minutes`, `auto_generate=True`, `max_context_meetings`, `max_attendee_history`, `briefing_ttl_hours`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_prep_config_sweep_defaults():
    from src.utils.config import PrepConfig

    cfg = PrepConfig()
    assert cfg.auto_generate is True
    assert cfg.lookahead_hours == 24
    assert cfg.sweep_interval_minutes == 15
    assert cfg.max_per_sweep == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_prep_config_sweep_defaults -v`
Expected: FAIL — `AttributeError: 'PrepConfig' object has no attribute 'lookahead_hours'`.

- [ ] **Step 3: Write minimal implementation**

In `src/utils/config.py`, replace the `PrepConfig` dataclass:

```python
@dataclass
class PrepConfig:
    lead_time_minutes: int = 15
    auto_generate: bool = True
    max_context_meetings: int = 3
    max_attendee_history: int = 5
    briefing_ttl_hours: int = 2
    lookahead_hours: int = 24          # NEW — sweep window
    sweep_interval_minutes: int = 15   # NEW
    max_per_sweep: int = 5             # NEW — cap generations per tick
```

In `config.example.yaml`, under the `prep:` section add the new keys (keep existing ones):

```yaml
prep:
  auto_generate: true # pre-generate briefings for upcoming meetings
  lookahead_hours: 24 # how far ahead the sweep looks
  sweep_interval_minutes: 15
  max_per_sweep: 5 # cap LLM generations per sweep tick
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py::test_prep_config_sweep_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_config.py
git commit -m "feat(config): PrepConfig sweep fields"
```

---

### Task 3: `PrepRepository` event-link methods

**Files:**

- Modify: `src/prep/repository.py`
- Test: `tests/test_prep_repository.py` (create if absent)

**Interfaces:**

- Consumes: `prep_briefings.calendar_event_uid`/`event_signature` (Task 1).
- Produces: `create(... calendar_event_uid=None, event_signature=None)`; `async has_current_for_event(uid, signature) -> bool`; `async get_by_calendar_event(uid) -> dict | None`; `async list_upcoming(limit=20) -> list[dict]`; `async prepared_event_uids() -> list[str]`.

- [ ] **Step 1: Write the failing test**

Create/extend `tests/test_prep_repository.py`:

```python
import time

import pytest

from src.prep.repository import PrepRepository


@pytest.fixture
async def prep_repo(db):
    return PrepRepository(db)


@pytest.mark.asyncio
async def test_create_with_event_link_and_lookup(prep_repo):
    future = time.time() + 3600
    bid = await prep_repo.create(
        content_markdown="brief",
        calendar_event_uid="EK1:1000",
        event_signature="sig-a",
        expires_at=future,
    )
    assert bid
    got = await prep_repo.get_by_calendar_event("EK1:1000")
    assert got is not None and got["content_markdown"] == "brief"
    assert await prep_repo.has_current_for_event("EK1:1000", "sig-a") is True
    assert await prep_repo.has_current_for_event("EK1:1000", "sig-DIFFERENT") is False
    assert await prep_repo.prepared_event_uids() == ["EK1:1000"]
    rows = await prep_repo.list_upcoming()
    assert [r["calendar_event_uid"] for r in rows] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_expired_event_briefing_is_not_current(prep_repo):
    past = time.time() - 10
    await prep_repo.create(
        content_markdown="old", calendar_event_uid="EK2:2000",
        event_signature="sig", expires_at=past,
    )
    assert await prep_repo.has_current_for_event("EK2:2000", "sig") is False
    assert await prep_repo.get_by_calendar_event("EK2:2000") is None
    assert await prep_repo.prepared_event_uids() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prep_repository.py -v`
Expected: FAIL — `create()` doesn't accept `calendar_event_uid`; the new methods don't exist.

- [ ] **Step 3: Write minimal implementation**

In `src/prep/repository.py`, extend `create` (add the two params + columns) and add the four methods. Replace the `create` method body's INSERT to include the new columns, and add params:

```python
    async def create(
        self,
        content_markdown: str,
        attendees_json: str = "[]",
        series_id: str | None = None,
        meeting_id: str | None = None,
        related_meeting_ids_json: str = "[]",
        open_action_items_json: str = "[]",
        expires_at: float | None = None,
        calendar_event_uid: str | None = None,
        event_signature: str | None = None,
    ) -> str:
        briefing_id = str(uuid.uuid4())
        now = time.time()
        if expires_at is None:
            expires_at = now + 7200  # 2 hours default TTL
        await self._db.conn.execute(
            """INSERT INTO prep_briefings
                (id, meeting_id, series_id, content_markdown, attendees_json,
                 related_meeting_ids_json, open_action_items_json, generated_at, expires_at,
                 calendar_event_uid, event_signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                briefing_id,
                meeting_id,
                series_id,
                content_markdown,
                attendees_json,
                related_meeting_ids_json,
                open_action_items_json,
                now,
                expires_at,
                calendar_event_uid,
                event_signature,
            ),
        )
        await self._db.conn.commit()
        return briefing_id
```

Add after the existing methods:

```python
    async def has_current_for_event(self, calendar_event_uid: str, event_signature: str) -> bool:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT 1 FROM prep_briefings "
            "WHERE calendar_event_uid = ? AND event_signature = ? AND expires_at > ? LIMIT 1",
            (calendar_event_uid, event_signature, now),
        )
        return await cursor.fetchone() is not None

    async def get_by_calendar_event(self, calendar_event_uid: str) -> dict | None:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings "
            "WHERE calendar_event_uid = ? AND expires_at > ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (calendar_event_uid, now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_upcoming(self, limit: int = 20) -> list[dict]:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings "
            "WHERE calendar_event_uid IS NOT NULL AND expires_at > ? "
            "ORDER BY generated_at DESC LIMIT ?",
            (now, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def prepared_event_uids(self) -> list[str]:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT DISTINCT calendar_event_uid FROM prep_briefings "
            "WHERE calendar_event_uid IS NOT NULL AND expires_at > ?",
            (now,),
        )
        return [r[0] for r in await cursor.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prep_repository.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/prep/repository.py tests/test_prep_repository.py
git commit -m "feat(prep): repository event-link methods"
```

---

### Task 4: Generator — LLM offload + event-link params

**Files:**

- Modify: `src/prep/briefing.py`
- Test: `tests/test_prep_briefing.py` (create if absent)

**Interfaces:**

- Consumes: `PrepRepository.create(... calendar_event_uid, event_signature)` (Task 3); `Summariser.chat(system, user) -> str`.
- Produces: `async generate(self, title, attendees, attendee_names, series_id=None, meeting_id=None, calendar_event_uid=None, event_signature=None, expires_at=None) -> str` — LLM call offloaded via `run_in_executor`, links the briefing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prep_briefing.py`:

```python
import pytest

from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository
from src.action_items.repository import ActionItemRepository
from src.series.repository import SeriesRepository
from src.utils.config import PrepConfig, SummarisationConfig


@pytest.fixture
async def generator(db, repo):
    gen = PrepBriefingGenerator(
        config=PrepConfig(),
        summarisation_config=SummarisationConfig(),
        meeting_repo=repo,
        action_item_repo=ActionItemRepository(db),
        series_repo=SeriesRepository(db),
        prep_repo=PrepRepository(db),
    )
    # Stub the LLM: no real model / network.
    gen._summariser.chat = lambda system, user: "## Prep\nstubbed briefing"
    return gen


@pytest.mark.asyncio
async def test_generate_links_calendar_event(generator, db):
    prep_repo = PrepRepository(db)
    future = 9999999999.0
    bid = await generator.generate(
        title="Weekly sync",
        attendees=["a@x.com"],
        attendee_names=["Alice"],
        calendar_event_uid="EK1:1000",
        event_signature="sig-a",
        expires_at=future,
    )
    assert bid
    got = await prep_repo.get_by_calendar_event("EK1:1000")
    assert got is not None
    assert got["event_signature"] == "sig-a"
    assert "stubbed briefing" in got["content_markdown"]


@pytest.mark.asyncio
async def test_generate_falls_back_on_llm_error(generator, db):
    def _boom(system, user):
        raise RuntimeError("llm down")

    generator._summariser.chat = _boom
    bid = await generator.generate(
        title="Weekly sync", attendees=["a@x.com"], attendee_names=["Alice"],
        calendar_event_uid="EK9:9000", event_signature="sig", expires_at=9999999999.0,
    )
    got = await PrepRepository(db).get_by_calendar_event("EK9:9000")
    assert got is not None  # fallback briefing still created + linked
    assert "Weekly sync" in got["content_markdown"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prep_briefing.py -v`
Expected: FAIL — `generate()` doesn't accept `calendar_event_uid`/`event_signature`/`expires_at`.

- [ ] **Step 3: Write minimal implementation**

In `src/prep/briefing.py`, add `import asyncio` at the top (next to `import json`), and replace the `generate` method:

```python
    async def generate(
        self,
        title: str,
        attendees: list[str],
        attendee_names: list[str],
        series_id: str | None = None,
        meeting_id: str | None = None,
        calendar_event_uid: str | None = None,
        event_signature: str | None = None,
        expires_at: float | None = None,
    ) -> str:
        context = await self.gather_context(attendees, series_id)
        user_msg = self._build_prompt(title, attendee_names, context)
        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(
                None, self._summariser.chat, PREP_PROMPT, user_msg
            )
        except Exception as e:
            logger.warning("Prep briefing generation failed: %s", e)
            content = self._build_fallback(title, context)

        briefing_id = await self._prep_repo.create(
            content_markdown=content,
            attendees_json=json.dumps(attendee_names),
            series_id=series_id,
            meeting_id=meeting_id,
            related_meeting_ids_json=json.dumps([m["id"] for m in context["series_meetings"]]),
            open_action_items_json=json.dumps(
                [{"id": i["id"], "title": i["title"]} for i in context["open_action_items"][:10]]
            ),
            calendar_event_uid=calendar_event_uid,
            event_signature=event_signature,
            expires_at=expires_at,
        )
        return briefing_id
```

(This replaces the private `_claude_chat`/`_ollama_chat` branch with the public `self._summariser.chat(...)` seam, run off the event loop — behaviour identical for callers, and the manual route + sweep both stay loop-safe.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prep_briefing.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/prep/briefing.py tests/test_prep_briefing.py
git commit -m "feat(prep): offload LLM + event-link params in generate()"
```

---

### Task 5: `PrepSweep` (qualification + orchestration)

**Files:**

- Create: `src/prep/sweep.py`
- Test: `tests/test_prep_sweep.py`

**Interfaces:**

- Consumes: `CalendarEventRepository.list_by_range(start, end) -> list[dict]` (rows with `event_uid`, `title`, `end_ts`, `attendees` list); `MeetingRepository.list_recent_complete_with_attendees(limit) -> list[dict]` (`attendees_json` string); `SeriesRepository.list_all() -> list[dict]` (`id`, `title`); `PrepRepository.has_current_for_event(uid, sig)`; `PrepBriefingGenerator.generate(...)`.
- Produces: module fns `event_signature(emails) -> str`, `attendee_history_match(event_emails, recent_meetings) -> bool`, `matched_series_id(event_title, series) -> str | None`; class `PrepSweep(generator, cal_event_repo, meeting_repo, series_repo, prep_repo, config)` with `async run(now: float) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_prep_sweep.py`:

```python
import pytest

from src.prep.sweep import (
    PrepSweep,
    attendee_history_match,
    event_signature,
    matched_series_id,
)


def test_event_signature_is_order_stable():
    assert event_signature(["B@x.com", "a@X.com"]) == event_signature(["a@x.com", "b@x.com"])
    assert event_signature(["a@x.com"]) != event_signature(["a@x.com", "b@x.com"])


def test_attendee_history_match():
    recent = [{"attendees_json": '[{"email": "a@x.com"}]'}]
    assert attendee_history_match({"a@x.com"}, recent) is True
    assert attendee_history_match({"z@x.com"}, recent) is False
    assert attendee_history_match(set(), recent) is False


def test_matched_series_id():
    series = [{"id": "s1", "title": "Weekly Sync"}]
    assert matched_series_id("weekly sync", series) == "s1"
    assert matched_series_id("Ad-hoc chat", series) is None


class _FakeGen:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return "bid"


class _FakeCalRepo:
    def __init__(self, events):
        self._events = events

    async def list_by_range(self, start, end):
        return [e for e in self._events if start <= e["start_ts"] < end]


class _FakeMeetingRepo:
    def __init__(self, recent):
        self._recent = recent

    async def list_recent_complete_with_attendees(self, limit=200):
        return self._recent


class _FakeSeriesRepo:
    def __init__(self, series):
        self._series = series

    async def list_all(self):
        return self._series


class _FakePrepRepo:
    def __init__(self):
        self.seen = set()

    async def has_current_for_event(self, uid, sig):
        return (uid, sig) in self.seen


class _Cfg:
    lookahead_hours = 24
    max_per_sweep = 5


def _event(uid, start, title="M", emails=("a@x.com", "b@x.com")):
    return {
        "event_uid": uid, "title": title, "start_ts": start, "end_ts": start + 1800.0,
        "attendees": [{"name": e.split("@")[0], "email": e} for e in emails],
    }


@pytest.mark.asyncio
async def test_sweep_generates_for_context_rich_and_skips_cold():
    now = 1000.0
    events = [
        _event("HIST:1100", 1100.0, emails=("a@x.com",)),   # attendee history -> qualifies
        _event("SER:1200", 1200.0, title="Weekly Sync", emails=("new@x.com",)),  # series -> qualifies
        _event("COLD:1300", 1300.0, emails=("nobody@x.com",)),  # cold -> skipped
    ]
    gen = _FakeGen()
    sweep = PrepSweep(
        generator=gen,
        cal_event_repo=_FakeCalRepo(events),
        meeting_repo=_FakeMeetingRepo([{"attendees_json": '[{"email": "a@x.com"}]'}]),
        series_repo=_FakeSeriesRepo([{"id": "s1", "title": "Weekly Sync"}]),
        prep_repo=_FakePrepRepo(),
        config=_Cfg(),
    )
    n = await sweep.run(now)
    assert n == 2
    uids = {c["calendar_event_uid"] for c in gen.calls}
    assert uids == {"HIST:1100", "SER:1200"}
    # series match threads the series_id through
    ser_call = next(c for c in gen.calls if c["calendar_event_uid"] == "SER:1200")
    assert ser_call["series_id"] == "s1"


@pytest.mark.asyncio
async def test_sweep_skips_already_briefed_and_caps_per_tick():
    now = 1000.0
    events = [_event(f"E:{1100+i}", 1100.0 + i, emails=("a@x.com",)) for i in range(4)]
    gen = _FakeGen()
    prep = _FakePrepRepo()
    # Mark the first event as already briefed (matching signature).
    prep.seen.add(("E:1100", event_signature(["a@x.com"])))
    cfg = _Cfg()
    cfg.max_per_sweep = 2
    sweep = PrepSweep(
        generator=gen, cal_event_repo=_FakeCalRepo(events),
        meeting_repo=_FakeMeetingRepo([{"attendees_json": '[{"email": "a@x.com"}]'}]),
        series_repo=_FakeSeriesRepo([]), prep_repo=prep, config=cfg,
    )
    n = await sweep.run(now)
    assert n == 2  # capped; and E:1100 was skipped as already briefed
    assert "E:1100" not in {c["calendar_event_uid"] for c in gen.calls}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prep_sweep.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.prep.sweep'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/prep/sweep.py`:

```python
"""Pre-generate prep briefings for upcoming context-rich calendar events."""

import hashlib
import json
import logging

logger = logging.getLogger("contextrecall.prep")


def event_signature(emails: list[str]) -> str:
    """Order-stable hash of the event's attendee emails (for change detection)."""
    normalized = ",".join(sorted(e.lower() for e in emails if e))
    return hashlib.sha1(normalized.encode()).hexdigest()


def attendee_history_match(event_emails: set[str], recent_meetings: list[dict]) -> bool:
    """True if any event attendee appears in a prior completed meeting."""
    if not event_emails:
        return False
    for m in recent_meetings:
        try:
            prior = {
                a.get("email", "").lower()
                for a in json.loads(m.get("attendees_json") or "[]")
                if a.get("email")
            }
        except (ValueError, TypeError):
            prior = set()
        if event_emails & prior:
            return True
    return False


def matched_series_id(event_title: str, series: list[dict]) -> str | None:
    """Return the id of a series whose title normalizes-equal to the event title."""
    t = (event_title or "").strip().casefold()
    if not t:
        return None
    for s in series:
        if (s.get("title") or "").strip().casefold() == t:
            return s.get("id")
    return None


class PrepSweep:
    """Generate briefings for upcoming context-rich events lacking a current one."""

    def __init__(self, generator, cal_event_repo, meeting_repo, series_repo, prep_repo, config):
        self._generator = generator
        self._cal_event_repo = cal_event_repo
        self._meeting_repo = meeting_repo
        self._series_repo = series_repo
        self._prep_repo = prep_repo
        self._config = config

    async def run(self, now: float) -> int:
        end = now + self._config.lookahead_hours * 3600
        events = await self._cal_event_repo.list_by_range(now, end)
        if not events:
            return 0
        recent = await self._meeting_repo.list_recent_complete_with_attendees(limit=200)
        series = await self._series_repo.list_all()
        generated = 0
        for event in events:
            if generated >= self._config.max_per_sweep:
                break
            attendees = event.get("attendees") or []
            emails = [a.get("email", "") for a in attendees if a.get("email")]
            email_set = {e.lower() for e in emails}
            sig = event_signature(emails)
            uid = event["event_uid"]
            if await self._prep_repo.has_current_for_event(uid, sig):
                continue
            sid = matched_series_id(event.get("title", ""), series)
            if not (attendee_history_match(email_set, recent) or sid is not None):
                continue
            names = [a.get("name", "") for a in attendees]
            try:
                await self._generator.generate(
                    title=event.get("title", ""),
                    attendees=emails,
                    attendee_names=names,
                    series_id=sid,
                    calendar_event_uid=uid,
                    event_signature=sig,
                    expires_at=event.get("end_ts"),
                )
                generated += 1
            except Exception:
                logger.exception("Prep generation failed for event %s", uid)
        return generated
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prep_sweep.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/prep/sweep.py tests/test_prep_sweep.py
git commit -m "feat(prep): PrepSweep qualification + orchestration"
```

---

### Task 6: Server wiring — generator + prep_sweep job

**Files:**

- Modify: `src/api/server.py`
- Test: `tests/test_server_prep_sweep.py`

**Interfaces:**

- Consumes: `PrepBriefingGenerator`, `PrepSweep`, repos, `config.prep`.
- Produces: `self._prep_generator` set at startup; `prep_routes.init(prep_repo, self._prep_generator)`; a `prep_sweep` scheduler job registered when `config.prep.auto_generate AND config.calendar.import_enabled`; `async def _sweep_prep(self)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_prep_sweep.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

from src.api.server import ApiServer


class _RecordingScheduler:
    def __init__(self):
        self.registered = []

    def register(self, name, func, interval):
        self.registered.append((name, interval))


def _config(auto=True, import_enabled=True):
    return SimpleNamespace(
        notifications=SimpleNamespace(enabled=False),
        analytics=SimpleNamespace(refresh_interval_hours=6),
        series=SimpleNamespace(heuristic_enabled=False),
        calendar=SimpleNamespace(import_enabled=import_enabled, sync_interval_minutes=15),
        prep=SimpleNamespace(auto_generate=auto, sweep_interval_minutes=15),
    )


def test_prep_sweep_registered_when_enabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(auto=True, import_enabled=True)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "prep_sweep" in names
    assert dict(server._scheduler.registered)["prep_sweep"] == 15 * 60


def test_prep_sweep_absent_when_auto_generate_off():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(auto=False, import_enabled=True)):
        server._setup_scheduler_jobs()
    assert "prep_sweep" not in [n for n, _ in server._scheduler.registered]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_server_prep_sweep.py -v`
Expected: FAIL — no `prep_sweep` job registered.

- [ ] **Step 3: Write minimal implementation**

In `src/api/server.py`:

(a) In `__init__` (near `self._calendar_sync = None`):

```python
        self._prep_generator = None
```

(b) In `_create_app`'s intelligence block: after `prep_repo = PrepRepository(self.db)` and the `from src.prep.repository import PrepRepository` group, construct the generator and replace `prep_routes.init(prep_repo)`:

```python
        from src.prep.briefing import PrepBriefingGenerator

        self._prep_generator = PrepBriefingGenerator(
            config=load_config().prep,
            summarisation_config=load_config().summarisation,
            meeting_repo=self.repo,
            action_item_repo=ai_repo,
            series_repo=series_repo,
            prep_repo=prep_repo,
        )
        prep_routes.init(prep_repo, self._prep_generator)
```

(Replace the existing `prep_routes.init(prep_repo)` line with the block above. `ai_repo` and `series_repo` are already constructed just above in the same block.)

(c) In `_setup_scheduler_jobs`, after the `calendar_sync` registration block:

```python
        if config.prep.auto_generate and config.calendar.import_enabled:
            self._scheduler.register(
                "prep_sweep",
                lambda: safe_run("prep_sweep", self._sweep_prep, timeout=300),
                config.prep.sweep_interval_minutes * 60,
            )
```

(d) Add the job method (near `_sync_calendar`):

```python
    async def _sweep_prep(self) -> None:
        """Pre-generate prep briefings for upcoming context-rich events."""
        import time

        generator = getattr(self, "_prep_generator", None)
        if generator is None:
            return
        from src.calendar_events.repository import CalendarEventRepository
        from src.prep.repository import PrepRepository
        from src.prep.sweep import PrepSweep
        from src.series.repository import SeriesRepository

        config = load_config().prep
        sweep = PrepSweep(
            generator=generator,
            cal_event_repo=CalendarEventRepository(self.db),
            meeting_repo=self.repo,
            series_repo=SeriesRepository(self.db),
            prep_repo=PrepRepository(self.db),
            config=config,
        )
        await sweep.run(time.time())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_server_prep_sweep.py -v`
Expected: PASS (both tests).
Then: `python3 -m pytest tests/test_api_prep.py tests/test_server_prep_sweep.py -v` (catch wiring regressions).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/server.py tests/test_server_prep_sweep.py
git commit -m "feat(api): wire prep generator + prep_sweep scheduler job"
```

---

### Task 7: API routes — upcoming-list + prepared-events

**Files:**

- Modify: `src/api/routes/prep.py`
- Test: `tests/test_api_prep.py` (create if absent)

**Interfaces:**

- Consumes: `PrepRepository.list_upcoming`, `prepared_event_uids`.
- Produces: `GET /api/prep/upcoming-list?limit=` → `list[dict]`; `GET /api/prep/prepared-events` → `{"event_uids": [...]}`. Both declared BEFORE the `/{meeting_id}` route.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_prep.py`:

```python
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import prep as prep_routes
from src.db.database import Database
from src.prep.repository import PrepRepository

TEST_TOKEN = "test-token-for-prep"


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
    db = Database(db_path=tmp_path / "prep_api.db")
    await db.connect()
    repo = PrepRepository(db)
    await repo.create(
        content_markdown="brief", calendar_event_uid="EK1:1000",
        event_signature="sig", expires_at=time.time() + 3600,
    )
    prep_routes.init(repo)
    app = FastAPI()
    app.include_router(prep_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db}
    await db.close()


@pytest.mark.asyncio
async def test_upcoming_list(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/upcoming-list", headers=_headers())
        assert r.status_code == 200
        assert [b["calendar_event_uid"] for b in r.json()] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_prepared_events(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/prep/prepared-events", headers=_headers())
        assert r.status_code == 200
        assert r.json() == {"event_uids": ["EK1:1000"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_prep.py -v`
Expected: FAIL — `/upcoming-list` is captured by `/{meeting_id}` (404 "No briefing found") and `/prepared-events` doesn't exist.

- [ ] **Step 3: Write minimal implementation**

In `src/api/routes/prep.py`, add these two routes **immediately after the existing `@router.get("/upcoming")` route and BEFORE `@router.get("/{meeting_id}")`**:

```python
@router.get("/upcoming-list")
async def get_upcoming_list(limit: int = 20):
    return await _get_repo().list_upcoming(limit)


@router.get("/prepared-events")
async def get_prepared_events():
    return {"event_uids": await _get_repo().prepared_event_uids()}
```

(`_get_repo()` is the existing helper that raises 503 if `_repo` is unset. Route ordering matters: literal paths must precede the `/{meeting_id}` catch-all.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_prep.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/prep.py tests/test_api_prep.py
git commit -m "feat(api): prep upcoming-list + prepared-events endpoints"
```

---

### Task 8: UI API client — prep list + prepared events

**Files:**

- Modify: `ui/src/lib/api.ts`
- Test: `ui/src/lib/__tests__/api.test.ts`

**Interfaces:**

- Consumes: `PrepBriefing` type (exists).
- Produces: `getUpcomingPrepList(limit?): Promise<PrepBriefing[]>`; `getPreparedEventUids(): Promise<{ event_uids: string[] }>`.

- [ ] **Step 1: Write the failing test**

Add to `ui/src/lib/__tests__/api.test.ts` (add `getUpcomingPrepList`, `getPreparedEventUids` to the import from `../api`):

```typescript
describe("auto-prep", () => {
  it("getUpcomingPrepList requests the list", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    await getUpcomingPrepList();
    expect(calls[0]).toContain("/api/prep/upcoming-list");
  });

  it("getPreparedEventUids requests prepared-events", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ event_uids: ["EK1:1000"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const res = await getPreparedEventUids();
    expect(res.event_uids).toEqual(["EK1:1000"]);
    expect(calls[0]).toContain("/api/prep/prepared-events");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- api.test`
Expected: FAIL — functions not exported.

- [ ] **Step 3: Write minimal implementation**

In `ui/src/lib/api.ts`, in the `// --- Prep Briefings ---` section add:

```typescript
export async function getUpcomingPrepList(limit = 20): Promise<PrepBriefing[]> {
  return request<PrepBriefing[]>(`/api/prep/upcoming-list?limit=${limit}`);
}

export async function getPreparedEventUids(): Promise<{
  event_uids: string[];
}> {
  return request<{ event_uids: string[] }>("/api/prep/prepared-events");
}
```

(`PrepBriefing` is already imported from `./types`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- api.test`
Expected: PASS.
Then: `cd ui && npx tsc --noEmit` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): prep list + prepared-events API client"
```

---

### Task 9: Prep view — upcoming briefings list

**Files:**

- Modify: `ui/src/components/prep/PrepBriefing.tsx`
- Test: `ui/src/components/prep/__tests__/PrepBriefing.test.tsx`

**Interfaces:**

- Consumes: `getUpcomingPrepList`, `PrepBriefing`.
- Produces: the `/prep` (no `meetingId`) path renders a **list** of upcoming briefings; the `/prep/:meetingId` path is unchanged.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/prep/__tests__/PrepBriefing.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { PrepBriefing } from "../PrepBriefing";

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("PrepBriefing upcoming list", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/prep/upcoming-list")) {
        return new Response(
          JSON.stringify([
            { id: "p1", meeting_id: null, series_id: null,
              content_markdown: "## Standup prep\nAlice notes", attendees_json: "[]",
              related_meeting_ids_json: "[]", open_action_items_json: "[]",
              generated_at: 1, expires_at: 9999999999 },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
  });

  it("renders the list of upcoming briefings", async () => {
    render(<PrepBriefing />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Standup prep")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- PrepBriefing`
Expected: FAIL — the no-`meetingId` path calls `getUpcomingPrep()` (single/204) and does not render the list.

- [ ] **Step 3: Write minimal implementation**

In `ui/src/components/prep/PrepBriefing.tsx`, change the no-`meetingId` branch to fetch and render a list. Add `getUpcomingPrepList` to the `../../lib/api` import, and split the component so that when there's no `meetingId` it renders the list:

```typescript
import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import Markdown from "react-markdown";
import {
  getUpcomingPrepList,
  getPrepForMeeting,
  generatePrep,
} from "../../lib/api";

function UpcomingList() {
  const { data: briefings = [], isLoading } = useQuery({
    queryKey: ["prep", "upcoming-list"],
    queryFn: () => getUpcomingPrepList(),
  });

  if (isLoading) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
      </div>
    );
  }
  if (briefings.length === 0) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <p className="text-sm text-text-muted text-center py-16">
          No upcoming briefings
        </p>
      </div>
    );
  }
  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      {briefings.map((b) => (
        <div
          key={b.id}
          className="rounded-xl border border-border bg-surface-raised p-5"
        >
          <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
            <Markdown>{b.content_markdown}</Markdown>
          </div>
        </div>
      ))}
    </div>
  );
}

export function PrepBriefing() {
  const { meetingId } = useParams<{ meetingId: string }>();
  const queryClient = useQueryClient();

  const {
    data: briefing,
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ["prep", meetingId],
    queryFn: () => getPrepForMeeting(meetingId!),
    enabled: !!meetingId,
  });

  const generate = useMutation({
    mutationFn: () => generatePrep(meetingId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prep", meetingId] });
      refetch();
    },
  });

  if (!meetingId) {
    return <UpcomingList />;
  }

  if (isLoading) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="h-6 w-48 bg-surface border border-border rounded animate-pulse mb-6" />
        <div className="space-y-3">
          <div className="h-4 w-full bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
          <div className="h-4 w-4/6 bg-surface border border-border rounded animate-pulse" />
        </div>
      </div>
    );
  }

  if (!briefing) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <p className="text-sm text-text-muted mb-4">No prep briefing available</p>
          <button
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            className="px-4 py-2 text-sm bg-accent text-white rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {generate.isPending ? "Generating..." : "Generate Briefing"}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
        <Markdown>{briefing.content_markdown}</Markdown>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- PrepBriefing`
Expected: PASS.
Then: `cd ui && npm test` and `cd ui && npx tsc --noEmit` — Expected: PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/prep/PrepBriefing.tsx ui/src/components/prep/__tests__/PrepBriefing.test.tsx
git commit -m "feat(ui): Prep view upcoming-briefings list"
```

---

### Task 10: Calendar "prep ready" badge

**Files:**

- Modify: `ui/src/components/calendar/CalendarView.tsx`, `UpcomingEventCard.tsx`, `MonthGrid.tsx`, `WeekTimeline.tsx`, `DayDetail.tsx`, `AgendaList.tsx`
- Test: `ui/src/components/calendar/__tests__/PrepBadge.test.tsx`

**Interfaces:**

- Consumes: `getPreparedEventUids`, `UpcomingEventCard`.
- Produces: an optional `preparedUids?: Set<string>` prop threaded from `CalendarView` through the grids to `UpcomingEventCard`, which shows a read-only "Prep ready" badge when `preparedUids.has(event.event_uid)`.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/PrepBadge.test.tsx`:

```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000", title: "Standup", start_ts: 1_700_000_000,
  end_ts: 1_700_003_600, attendees: [], organizer: null, join_url: "",
  meeting_id: "", calendar_name: "Work",
};

describe("UpcomingEventCard prep badge", () => {
  it("shows a Prep ready badge when the uid is prepared", () => {
    render(<UpcomingEventCard event={EVENT} preparedUids={new Set(["EK1:1000"])} />);
    expect(screen.getByText(/Prep ready/i)).toBeInTheDocument();
  });

  it("hides the badge when not prepared", () => {
    render(<UpcomingEventCard event={EVENT} preparedUids={new Set()} />);
    expect(screen.queryByText(/Prep ready/i)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- PrepBadge`
Expected: FAIL — `UpcomingEventCard` has no `preparedUids` prop / no badge.

- [ ] **Step 3: Write minimal implementation**

In `UpcomingEventCard.tsx`, add the optional prop and render the badge (inside the button, after the title):

```typescript
interface UpcomingEventCardProps {
  event: CalendarEvent;
  compact?: boolean;
  preparedUids?: Set<string>;
}

export function UpcomingEventCard({ event, compact = false, preparedUids }: UpcomingEventCardProps) {
  // ...existing useState/title/start...
  const prepared = preparedUids?.has(event.event_uid) ?? false;
```

Inside the button's `<span>` block (after the title text), add:

```tsx
{
  prepared && (
    <span className="ml-1 rounded bg-accent/20 text-accent px-1 text-[9px] align-middle">
      Prep ready
    </span>
  );
}
```

Thread the prop through each grid: add `preparedUids?: Set<string>` to `MonthGridProps`, `WeekTimelineProps`, `DayDetailProps`, `AgendaListProps` (optional), and pass `preparedUids={preparedUids}` to every `<UpcomingEventCard .../>` render in those files.

In `CalendarView.tsx`, add a parallel query and thread it:

```typescript
const { data: preparedData } = useQuery({
  queryKey: ["prepared-events"],
  queryFn: () => getPreparedEventUids(),
  enabled: daemonRunning,
  staleTime: 30_000,
});
const preparedUids = useMemo(
  () => new Set(preparedData?.event_uids ?? []),
  [preparedData],
);
```

(import `getPreparedEventUids` from `../../lib/api`; `useMemo` from `react`) and pass `preparedUids={preparedUids}` to `MonthGrid`, `WeekTimeline`, `DayDetail`, `AgendaList` alongside the existing `events={events}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- PrepBadge`
Expected: PASS.
Then: `cd ui && npm test` and `cd ui && npx tsc --noEmit` — Expected: PASS (existing calendar tests still pass; the prop is optional).

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/
git commit -m "feat(ui): 'prep ready' badge on upcoming calendar events"
```

---

### Task 11: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Python suite + lint**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all, including the new prep tests). Note any pre-existing unrelated failure but do not fix here.
Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 2: UI suite + types**

Run: `cd ui && npm test`
Expected: PASS.
Run: `cd ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit (only if lint/format fixups were needed)**

```bash
git add -A
git commit -m "chore(auto-prep): lint + test fixups"
```

(Skip if nothing changed.)

---

## Self-Review

**Spec coverage:**

- Scheduler pre-gen sweep → Tasks 5 (`PrepSweep`), 6 (job). ✔
- Context-rich filter (attendee-history OR series-title) → Task 5 (`attendee_history_match` / `matched_series_id`). ✔
- Wire orphaned generator (+ fix manual 503) → Task 6 (`prep_routes.init(prep_repo, generator)`). ✔
- LLM offload → Task 4 (`run_in_executor(self._summariser.chat, ...)`). ✔
- Link to event + regen-on-change (uid + signature) → Tasks 1 (columns), 3 (`has_current_for_event`), 4/5 (write signature). ✔
- 24h lookahead / max_per_sweep / config → Tasks 2, 5, 6. ✔
- `expires_at = event.end_ts` → Tasks 4/5 (passed through). ✔
- Prep-view list → Tasks 7 (`upcoming-list`), 8, 9. ✔
- "Prep ready" badge → Tasks 7 (`prepared-events`), 8, 10. ✔
- Migration v19 fresh + upgrade → Task 1. ✔
- config.example.yaml → Task 2. ✔

**Placeholder scan:** none — every step carries concrete code/commands.

**Type consistency:** `generate(..., calendar_event_uid, event_signature, expires_at)` used identically in Tasks 4, 5, 6. `PrepRepository.create(... calendar_event_uid, event_signature)` consistent Tasks 3, 4. `has_current_for_event(uid, sig)` used in Tasks 3, 5. `PrepSweep(generator, cal_event_repo, meeting_repo, series_repo, prep_repo, config)` consistent Tasks 5, 6. `event_signature(emails)` consistent Tasks 5. TS `getUpcomingPrepList`/`getPreparedEventUids` consistent Tasks 8, 9, 10. `preparedUids: Set<string>` consistent Task 10.

**Note (route ordering, Task 7):** `/upcoming-list` and `/prepared-events` MUST precede `/{meeting_id}` in `prep.py` — called out in the task and Global Constraints.
