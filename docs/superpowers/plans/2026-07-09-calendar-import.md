# Calendar Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import upcoming _meeting-like_ macOS calendar events and merge them into the existing calendar UI, backed by a persisted rolling window that later phases (auto-prep, auto-arm) will consume.

**Architecture:** A new `CalendarReader` (macOS EventKit, reusing `CalendarMatcher`'s pure helpers) reads events for a date range. The UI reads live per visible range via `GET /api/calendar/events`; a background scheduler job mirrors a rolling near-term window into a new `calendar_events` table (migration v18) via `CalendarSyncJob`. React grids gain an optional `events` prop and render upcoming events distinctly.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pyobjc EventKit (optional/guarded), pytest + pytest-asyncio; React 19 + TypeScript, TanStack Query, Vitest 4, date-fns, Tailwind 4.

## Global Constraints

- **Migration head is currently `SCHEMA_VERSION = 17`** — this feature bumps it to **18**. New table DDL goes in TWO places in `src/db/database.py`: the fresh-install `if current_version < 1:` block AND a new `if current_version < 18:` block; move the trailing `else: logger.debug(...)` after the new block.
- **EventKit is unavailable in tests/CI** (`tests/conftest.py`). All EventKit access must be guarded so pure logic is testable without it; `CalendarReader` methods return empty lists when EventKit is unavailable.
- **Repository writes** go through `async with self._db.write_lock:` and `await self._db.conn.commit()` inside that block. Reads use `self._db.conn.execute(...)` with no lock. Rows convert via `dict(row)` (row_factory is `aiosqlite.Row`).
- **API routers** use a bare `APIRouter()` with full `/api/...` paths; module-level repo/service globals set via `def init(...)`; auth is applied at `include_router(..., dependencies=[Depends(verify_token)])`.
- **Scheduler jobs** are registered as `self._scheduler.register("name", lambda: safe_run("name", self._coro_method), interval_seconds)`; blocking work inside the async job is offloaded with `await asyncio.get_running_loop().run_in_executor(None, fn, *args)`.
- **UI API fns** are `export async function` using `request<T>(path, options?)`; paths start with `/api/...`; timestamps are UNIX SECONDS converted via `new Date(ts * 1000)`; the per-day key is `format(new Date(ts * 1000), "yyyy-MM-dd")`.
- **Config:** only `src/utils/config.py` is edited (define fields on the existing `CalendarConfig`); the config route auto-derives sections from `AppConfig`, so no route edits.
- **Test async style:** migration tests are plain `async def test_...(tmp_path)` (no decorator, auto mode); repository/API/sync tests use `@pytest.mark.asyncio`. Match the neighbouring file.
- **Commands:** Python tests `python3 -m pytest <path> -v`; lint `ruff check src/ tests/`. UI `cd ui && npm test`; types `cd ui && npx tsc --noEmit`.

---

### Task 1: Migration v18 — `calendar_events` table

**Files:**

- Modify: `src/db/database.py` (SCHEMA_VERSION, add `CALENDAR_EVENTS_SQL`, two migration blocks)
- Test: `tests/test_db_migration_v18.py`

**Interfaces:**

- Produces: table `calendar_events` (PK `event_uid TEXT`, columns `title, start_ts, end_ts, attendees_json, organizer_json, join_url, meeting_id, calendar_name, recorded_meeting_id, synced_at`, index on `start_ts`); `SCHEMA_VERSION == 18`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration_v18.py`:

```python
import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v18_creates_calendar_events_table(tmp_path):
    db = Database(db_path=tmp_path / "v18.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 18
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_events'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("PRAGMA table_info(calendar_events)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {
            "event_uid", "title", "start_ts", "end_ts", "attendees_json",
            "organizer_json", "join_url", "meeting_id", "calendar_name",
            "recorded_meeting_id", "synced_at",
        } <= cols
    finally:
        await db.close()


async def test_v18_upgrade_from_v17_preserves_data(tmp_path):
    db_path = tmp_path / "v17old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, started_at REAL)")
        await conn.execute("INSERT INTO meetings (id, started_at) VALUES ('m1', 1.0)")
        await conn.execute("PRAGMA user_version = 17")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='calendar_events'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("SELECT id FROM meetings WHERE id='m1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v18.py -v`
Expected: FAIL — `SCHEMA_VERSION` is 17 (`assert 17 >= 18`) and no `calendar_events` table.

- [ ] **Step 3: Write minimal implementation**

In `src/db/database.py`:

(a) Bump the head version (line ~25):

```python
SCHEMA_VERSION = 18
```

(b) Add the DDL constant near the other `*_SQL` constants (e.g. after `AUTOMATION_DISPATCHES_SQL`):

```python
CALENDAR_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS calendar_events (
    event_uid TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    attendees_json TEXT NOT NULL DEFAULT '[]',
    organizer_json TEXT,
    join_url TEXT NOT NULL DEFAULT '',
    meeting_id TEXT NOT NULL DEFAULT '',
    calendar_name TEXT NOT NULL DEFAULT '',
    recorded_meeting_id TEXT,
    synced_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_start ON calendar_events(start_ts);
"""
```

(c) In the fresh-install block (`if current_version < 1:`), append after the `# Automations (v17).` lines and before `await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")`:

```python
            # Calendar import (v18).
            await self.conn.executescript(CALENDAR_EVENTS_SQL)
```

(d) Replace the trailing block. Find:

```python
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

and change it to:

```python
        if current_version < 17:
            # Automations: user-defined condition→action rules.
            await self.conn.executescript(AUTOMATION_RULES_SQL)
            await self.conn.executescript(AUTOMATION_DISPATCHES_SQL)
            await self.conn.execute("PRAGMA user_version = 17")
            await self.conn.commit()
            logger.info("Database migrated to version 17 (automations)")
            current_version = 17

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

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db_migration_v18.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/db/database.py tests/test_db_migration_v18.py
git commit -m "feat(db): calendar_events table (v18)"
```

---

### Task 2: `CalendarConfig` fields + config example

**Files:**

- Modify: `src/utils/config.py` (`CalendarConfig` dataclass)
- Modify: `config.example.yaml`
- Test: `tests/test_config.py` (add a defaults test)

**Interfaces:**

- Produces: `CalendarConfig` with new fields `import_enabled: bool = True`, `sync_interval_minutes: int = 15`, `sync_horizon_days: int = 21`, `excluded_calendars: list[str] = []`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_calendar_config_import_defaults():
    from src.utils.config import CalendarConfig

    cfg = CalendarConfig()
    assert cfg.import_enabled is True
    assert cfg.sync_interval_minutes == 15
    assert cfg.sync_horizon_days == 21
    assert cfg.excluded_calendars == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_calendar_config_import_defaults -v`
Expected: FAIL — `AttributeError: 'CalendarConfig' object has no attribute 'import_enabled'`.

- [ ] **Step 3: Write minimal implementation**

In `src/utils/config.py`, replace the `CalendarConfig` dataclass:

```python
@dataclass
class CalendarConfig:
    enabled: bool = False
    time_window_minutes: int = 15
    min_confidence: float = 0.7
    import_enabled: bool = True
    sync_interval_minutes: int = 15
    sync_horizon_days: int = 21
    excluded_calendars: list[str] = field(default_factory=list)
```

(`field` and `dataclass` are already imported at the top of this module. No change needed to `AppConfig` or `load_config` — the `calendar=` line already exists.)

In `config.example.yaml`, under the existing `calendar:` section add the new keys (find `calendar:` and append):

```yaml
calendar:
  enabled: false
  time_window_minutes: 15
  min_confidence: 0.7
  # Calendar import (Track B): mirror upcoming meeting-like events into the app.
  import_enabled: true
  sync_interval_minutes: 15
  sync_horizon_days: 21
  excluded_calendars: [] # calendar names to skip, e.g. ["Personal"]
```

(If `calendar:` already lists the first three keys, keep them and add only the new ones.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py::test_calendar_config_import_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py config.example.yaml tests/test_config.py
git commit -m "feat(config): CalendarConfig import fields"
```

---

### Task 3: `CalendarReader` (EventKit range reader)

**Files:**

- Create: `src/calendar_events/__init__.py`
- Create: `src/calendar_events/reader.py`
- Test: `tests/test_calendar_reader.py`

**Interfaces:**

- Consumes: pure helpers from `src/calendar_matcher.py` — `TEAMS_URL_PATTERN`, `_extract_teams_details(text) -> (join_url, meeting_id)`, `_extract_attendee_info(participant) -> dict | None`, `_is_eventkit_available() -> bool`.
- Produces:
  - `@dataclass CalendarEvent(event_uid, title, start_ts, end_ts, attendees, organizer, join_url, meeting_id, calendar_name)` with `to_dict() -> dict`.
  - `def is_meeting_like(join_url: str, attendees: list) -> bool`
  - `def _events_from_extracted(extracted: list[dict], excluded_calendars: set[str]) -> list[CalendarEvent]`
  - `class CalendarReader` with `def __init__(self, excluded_calendars: list[str] | None = None)`, `property available -> bool`, `def list_events(self, start: float, end: float, excluded_calendars: list[str] | None = None) -> list[CalendarEvent]`, `def list_calendars(self) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_calendar_reader.py`:

```python
from src.calendar_events.reader import (
    CalendarEvent,
    CalendarReader,
    _events_from_extracted,
    is_meeting_like,
)


def _extracted(**over):
    base = {
        "event_identifier": "EK1",
        "title": "Sync",
        "start_ts": 1000.0,
        "end_ts": 2000.0,
        "attendees": [{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@x.com"}],
        "organizer": None,
        "join_url": "",
        "meeting_id": "",
        "calendar_name": "Work",
        "is_all_day": False,
    }
    base.update(over)
    return base


def test_is_meeting_like():
    assert is_meeting_like("https://teams...", []) is True
    assert is_meeting_like("", [{"name": "A"}, {"name": "B"}]) is True
    assert is_meeting_like("", [{"name": "A"}]) is False
    assert is_meeting_like("", []) is False


def test_event_uid_synthesised_from_identifier_and_start():
    events = _events_from_extracted([_extracted()], set())
    assert len(events) == 1
    assert events[0].event_uid == "EK1:1000"
    assert events[0].to_dict()["event_uid"] == "EK1:1000"


def test_all_day_events_skipped():
    assert _events_from_extracted([_extracted(is_all_day=True)], set()) == []


def test_non_meeting_like_skipped():
    assert _events_from_extracted([_extracted(attendees=[{"name": "A"}], join_url="")], set()) == []


def test_excluded_calendar_skipped():
    assert _events_from_extracted([_extracted(calendar_name="Personal")], {"Personal"}) == []


def test_events_sorted_by_start():
    out = _events_from_extracted(
        [_extracted(event_identifier="B", start_ts=3000.0),
         _extracted(event_identifier="A", start_ts=1000.0)],
        set(),
    )
    assert [e.start_ts for e in out] == [1000.0, 3000.0]


def test_reader_unavailable_returns_empty_without_eventkit():
    reader = CalendarReader()
    # In CI EventKit is unavailable, so the reader is not available.
    assert reader.available is False
    assert reader.list_events(0.0, 10_000.0) == []
    assert reader.list_calendars() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calendar_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.calendar_events'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/calendar_events/__init__.py`:

```python
"""Calendar import (Track B): upcoming-event reader, repository, and sync."""
```

Create `src/calendar_events/reader.py`:

```python
"""Read upcoming meeting-like events from macOS Calendar via EventKit.

Reuses the pure extraction helpers from src.calendar_matcher so the reactive
matcher and this range reader share one definition of attendee/Teams parsing.
All EventKit access is guarded: without EventKit (e.g. CI) the reader is simply
`available == False` and every read returns an empty list.
"""

import logging
import threading
from dataclasses import asdict, dataclass, field

from src.calendar_matcher import (
    _extract_attendee_info,
    _extract_teams_details,
    _is_eventkit_available,
)

logger = logging.getLogger("contextrecall.calendar_events")


@dataclass
class CalendarEvent:
    event_uid: str
    title: str
    start_ts: float
    end_ts: float
    attendees: list = field(default_factory=list)
    organizer: dict | None = None
    join_url: str = ""
    meeting_id: str = ""
    calendar_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_meeting_like(join_url: str, attendees: list) -> bool:
    """A calendar event counts as a meeting if it has a join link or >=2 attendees."""
    return bool(join_url) or len(attendees or []) >= 2


def _events_from_extracted(extracted: list[dict], excluded_calendars: set[str]) -> list[CalendarEvent]:
    """Pure transform: filter extracted event dicts and build CalendarEvents.

    Skips all-day events, excluded calendars, and non-meeting-like events.
    The event_uid is synthesised as ``<eventIdentifier>:<int(start_ts)>`` because
    EventKit's eventIdentifier is shared across recurring occurrences.
    """
    events: list[CalendarEvent] = []
    for e in extracted:
        if e.get("is_all_day"):
            continue
        if e.get("calendar_name", "") in excluded_calendars:
            continue
        join_url = e.get("join_url", "") or ""
        attendees = e.get("attendees") or []
        if not is_meeting_like(join_url, attendees):
            continue
        start_ts = float(e["start_ts"])
        events.append(
            CalendarEvent(
                event_uid=f"{e['event_identifier']}:{int(start_ts)}",
                title=e.get("title", "") or "",
                start_ts=start_ts,
                end_ts=float(e.get("end_ts", start_ts)),
                attendees=attendees,
                organizer=e.get("organizer"),
                join_url=join_url,
                meeting_id=e.get("meeting_id", "") or "",
                calendar_name=e.get("calendar_name", "") or "",
            )
        )
    events.sort(key=lambda ev: ev.start_ts)
    return events


class CalendarReader:
    """Range reader over macOS Calendar events. EventKit access is lazy + guarded."""

    def __init__(self, excluded_calendars: list[str] | None = None) -> None:
        self._excluded = set(excluded_calendars or [])
        self._store = None
        self._authorized = False
        self._init_attempted = False

    def _ensure_store(self) -> None:
        """Lazily create the EventKit store and request access (blocking auth wait).

        Must be called from a worker thread (the API server offloads reads via
        run_in_executor), never on the event loop.
        """
        if self._init_attempted:
            return
        self._init_attempted = True
        if not _is_eventkit_available():
            return
        try:
            import EventKit

            self._store = EventKit.EKEventStore.alloc().init()
            done = threading.Event()
            result = [False]

            def on_access(granted, error):
                result[0] = granted
                if error:
                    logger.warning("Calendar access error: %s", error)
                done.set()

            self._store.requestAccessToEntityType_completion_(
                EventKit.EKEntityTypeEvent, on_access
            )
            if done.wait(timeout=60):
                self._authorized = result[0]
            else:
                logger.warning("Calendar access request timed out")
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Failed to initialise EventKit reader: %s", e)
            self._store = None

    @property
    def available(self) -> bool:
        return self._store is not None and self._authorized

    def _extract(self, event) -> dict | None:  # pragma: no cover - requires EventKit
        """Extract a plain dict from an EKEvent (the only EventKit-specific step)."""
        try:
            attendees = []
            raw = event.attendees()
            if raw:
                for p in raw:
                    info = _extract_attendee_info(p)
                    if not info:
                        continue
                    try:
                        if p.isCurrentUser():
                            continue
                    except Exception:
                        pass
                    attendees.append(info)
            organizer = None
            try:
                org = event.organizer()
                if org:
                    organizer = _extract_attendee_info(org)
            except Exception:
                pass
            join_url, meeting_id = "", ""
            for getter in (event.URL, event.notes, event.location):
                try:
                    val = getter()
                    if not val:
                        continue
                    text = str(val.absoluteString() if hasattr(val, "absoluteString") else val)
                    ju, mid = _extract_teams_details(text)
                    if ju:
                        join_url, meeting_id = ju, mid
                        break
                except Exception:
                    continue
            cal = ""
            try:
                cal = str(event.calendar().title() or "")
            except Exception:
                pass
            return {
                "event_identifier": str(event.eventIdentifier() or ""),
                "title": str(event.title() or ""),
                "start_ts": float(event.startDate().timeIntervalSince1970()),
                "end_ts": float(event.endDate().timeIntervalSince1970()),
                "attendees": attendees,
                "organizer": organizer,
                "join_url": join_url,
                "meeting_id": meeting_id,
                "calendar_name": cal,
                "is_all_day": bool(event.isAllDay()),
            }
        except Exception:
            return None

    def list_events(
        self, start: float, end: float, excluded_calendars: list[str] | None = None
    ) -> list[CalendarEvent]:
        """Return meeting-like events in [start, end). Empty if EventKit unavailable."""
        self._ensure_store()
        if not self.available:
            return []
        excluded = set(excluded_calendars) if excluded_calendars is not None else self._excluded
        try:  # pragma: no cover - requires EventKit
            from Foundation import NSDate

            ns_start = NSDate.dateWithTimeIntervalSince1970_(start)
            ns_end = NSDate.dateWithTimeIntervalSince1970_(end)
            predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                ns_start, ns_end, None
            )
            raw = self._store.eventsMatchingPredicate_(predicate) or []
            extracted = [x for x in (self._extract(e) for e in raw) if x]
            return _events_from_extracted(extracted, excluded)
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Calendar list_events failed: %s", e)
            return []

    def list_calendars(self) -> list[dict]:
        """Return [{id, title}] for every event calendar. Empty if unavailable."""
        self._ensure_store()
        if not self.available:
            return []
        try:  # pragma: no cover - requires EventKit
            import EventKit

            cals = self._store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []
            return [
                {"id": str(c.calendarIdentifier() or ""), "title": str(c.title() or "")}
                for c in cals
            ]
        except Exception as e:  # pragma: no cover - requires EventKit
            logger.warning("Calendar list_calendars failed: %s", e)
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_calendar_reader.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/calendar_events/__init__.py src/calendar_events/reader.py tests/test_calendar_reader.py
git commit -m "feat(calendar): CalendarReader for upcoming events"
```

---

### Task 4: `CalendarEventRepository`

**Files:**

- Create: `src/calendar_events/repository.py`
- Test: `tests/test_calendar_event_repository.py`

**Interfaces:**

- Consumes: `Database` (via `self._db.conn` + `self._db.write_lock`); `CalendarEvent` from `src.calendar_events.reader`.
- Produces: `class CalendarEventRepository` with `__init__(self, db: Database)`, `async def upsert(self, event: CalendarEvent) -> None`, `async def list_by_range(self, start: float, end: float) -> list[dict]`, `async def prune_window(self, start: float, end: float, keep_uids: set[str]) -> int`, `async def set_recorded_meeting(self, event_uid: str, meeting_id: str) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_calendar_event_repository.py`:

```python
import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository


@pytest.fixture
async def cal_repo(db):
    return CalendarEventRepository(db)


def _ev(uid="EK1:1000", start=1000.0, title="Sync"):
    return CalendarEvent(
        event_uid=uid, title=title, start_ts=start, end_ts=start + 1800.0,
        attendees=[{"name": "A", "email": "a@x.com"}], organizer=None,
        join_url="https://teams", meeting_id="19:abc", calendar_name="Work",
    )


@pytest.mark.asyncio
async def test_upsert_and_list_by_range(cal_repo):
    await cal_repo.upsert(_ev())
    rows = await cal_repo.list_by_range(0.0, 10_000.0)
    assert len(rows) == 1
    assert rows[0]["event_uid"] == "EK1:1000"
    assert rows[0]["attendees"] == [{"name": "A", "email": "a@x.com"}]
    assert rows[0]["join_url"] == "https://teams"


@pytest.mark.asyncio
async def test_upsert_updates_existing_but_preserves_recorded_link(cal_repo):
    await cal_repo.upsert(_ev(title="Sync"))
    await cal_repo.set_recorded_meeting("EK1:1000", "m1")
    await cal_repo.upsert(_ev(title="Renamed"))  # re-sync same uid
    rows = await cal_repo.list_by_range(0.0, 10_000.0)
    assert rows[0]["title"] == "Renamed"
    assert rows[0]["recorded_meeting_id"] == "m1"  # not clobbered by upsert


@pytest.mark.asyncio
async def test_list_by_range_excludes_out_of_window(cal_repo):
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    await cal_repo.upsert(_ev(uid="EK2:9000", start=9000.0))
    rows = await cal_repo.list_by_range(0.0, 5000.0)
    assert [r["event_uid"] for r in rows] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_prune_window_removes_absent_but_keeps_recorded(cal_repo):
    await cal_repo.upsert(_ev(uid="EK1:1000", start=1000.0))
    await cal_repo.upsert(_ev(uid="EK2:2000", start=2000.0))
    await cal_repo.set_recorded_meeting("EK2:2000", "m2")
    # Only EK1 is still present in the fresh fetch; EK2 vanished but is recorded.
    removed = await cal_repo.prune_window(0.0, 5000.0, keep_uids={"EK1:1000"})
    assert removed == 0  # EK2 kept because it has a recorded_meeting_id
    await cal_repo.upsert(_ev(uid="EK3:3000", start=3000.0))
    removed = await cal_repo.prune_window(0.0, 5000.0, keep_uids={"EK1:1000"})
    assert removed == 1  # EK3 pruned; EK2 still kept
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 5000.0)}
    assert rows == {"EK1:1000", "EK2:2000"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calendar_event_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.calendar_events.repository'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/calendar_events/repository.py`:

```python
"""Async CRUD for the calendar_events mirror table (Track B foundation)."""

import json
import logging
import time

from src.calendar_events.reader import CalendarEvent
from src.db.database import Database

logger = logging.getLogger("contextrecall.calendar_events")


class CalendarEventRepository:
    """Persisted rolling window of upcoming calendar events."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, event: CalendarEvent) -> None:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO calendar_events "
                "(event_uid, title, start_ts, end_ts, attendees_json, organizer_json, "
                "join_url, meeting_id, calendar_name, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_uid) DO UPDATE SET "
                "title=excluded.title, start_ts=excluded.start_ts, end_ts=excluded.end_ts, "
                "attendees_json=excluded.attendees_json, organizer_json=excluded.organizer_json, "
                "join_url=excluded.join_url, meeting_id=excluded.meeting_id, "
                "calendar_name=excluded.calendar_name, synced_at=excluded.synced_at",
                (
                    event.event_uid,
                    event.title,
                    event.start_ts,
                    event.end_ts,
                    json.dumps(event.attendees or []),
                    json.dumps(event.organizer) if event.organizer else None,
                    event.join_url,
                    event.meeting_id,
                    event.calendar_name,
                    now,
                ),
            )
            await self._db.conn.commit()

    async def list_by_range(self, start: float, end: float) -> list[dict]:
        cur = await self._db.conn.execute(
            "SELECT * FROM calendar_events WHERE start_ts >= ? AND start_ts < ? "
            "ORDER BY start_ts",
            (start, end),
        )
        return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def prune_window(self, start: float, end: float, keep_uids: set[str]) -> int:
        cur = await self._db.conn.execute(
            "SELECT event_uid FROM calendar_events "
            "WHERE start_ts >= ? AND start_ts < ? AND recorded_meeting_id IS NULL",
            (start, end),
        )
        stale = [r[0] for r in await cur.fetchall() if r[0] not in keep_uids]
        if not stale:
            return 0
        async with self._db.write_lock:
            await self._db.conn.executemany(
                "DELETE FROM calendar_events WHERE event_uid = ?",
                [(uid,) for uid in stale],
            )
            await self._db.conn.commit()
        return len(stale)

    async def set_recorded_meeting(self, event_uid: str, meeting_id: str) -> None:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE calendar_events SET recorded_meeting_id = ? WHERE event_uid = ?",
                (meeting_id, event_uid),
            )
            await self._db.conn.commit()

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["attendees"] = json.loads(d.pop("attendees_json") or "[]")
        except (ValueError, TypeError):
            d["attendees"] = []
        try:
            d["organizer"] = json.loads(d.pop("organizer_json")) if d.get("organizer_json") else None
        except (ValueError, TypeError):
            d["organizer"] = None
        d.pop("organizer_json", None)
        return d
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_calendar_event_repository.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/calendar_events/repository.py tests/test_calendar_event_repository.py
git commit -m "feat(calendar): CalendarEventRepository (upsert/prune/list)"
```

---

### Task 5: `CalendarSyncJob.apply` (mirror logic)

**Files:**

- Create: `src/calendar_events/sync.py`
- Test: `tests/test_calendar_sync_job.py`

**Interfaces:**

- Consumes: `CalendarEventRepository`; `CalendarEvent`.
- Produces: `class CalendarSyncJob` with `__init__(self, repo: CalendarEventRepository)`, `async def apply(self, window_start: float, window_end: float, events: list[CalendarEvent]) -> int` — upserts each event, prunes window rows whose uid is absent from `events` (except recorded), returns the number of events upserted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_calendar_sync_job.py`:

```python
import pytest

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_events.sync import CalendarSyncJob


@pytest.fixture
async def cal_repo(db):
    return CalendarEventRepository(db)


def _ev(uid, start):
    return CalendarEvent(
        event_uid=uid, title="M", start_ts=start, end_ts=start + 1800.0,
        attendees=[{"name": "A"}, {"name": "B"}], organizer=None,
        join_url="", meeting_id="", calendar_name="Work",
    )


@pytest.mark.asyncio
async def test_apply_upserts_events(cal_repo):
    job = CalendarSyncJob(cal_repo)
    n = await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0), _ev("B:2000", 2000.0)])
    assert n == 2
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"A:1000", "B:2000"}


@pytest.mark.asyncio
async def test_apply_prunes_events_no_longer_present(cal_repo):
    job = CalendarSyncJob(cal_repo)
    await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0), _ev("B:2000", 2000.0)])
    # Second sync: B is gone (cancelled/moved).
    await job.apply(0.0, 10_000.0, [_ev("A:1000", 1000.0)])
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"A:1000"}


@pytest.mark.asyncio
async def test_apply_does_not_prune_outside_window(cal_repo):
    job = CalendarSyncJob(cal_repo)
    await cal_repo.upsert(_ev("OLD:100", 100.0))  # before the window
    await job.apply(500.0, 10_000.0, [_ev("A:1000", 1000.0)])
    rows = {r["event_uid"] for r in await cal_repo.list_by_range(0.0, 10_000.0)}
    assert rows == {"OLD:100", "A:1000"}  # OLD untouched (outside sync window)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calendar_sync_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.calendar_events.sync'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/calendar_events/sync.py`:

```python
"""Apply a fetched window of calendar events to the mirror table."""

import logging

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository

logger = logging.getLogger("contextrecall.calendar_events")


class CalendarSyncJob:
    """Upsert a fetched window of events and prune those that vanished from it."""

    def __init__(self, repo: CalendarEventRepository) -> None:
        self._repo = repo

    async def apply(
        self, window_start: float, window_end: float, events: list[CalendarEvent]
    ) -> int:
        for event in events:
            await self._repo.upsert(event)
        keep = {e.event_uid for e in events}
        removed = await self._repo.prune_window(window_start, window_end, keep)
        logger.debug(
            "Calendar sync applied: %d upserted, %d pruned (window %.0f-%.0f)",
            len(events), removed, window_start, window_end,
        )
        return len(events)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_calendar_sync_job.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/calendar_events/sync.py tests/test_calendar_sync_job.py
git commit -m "feat(calendar): CalendarSyncJob mirror apply/prune"
```

---

### Task 6: API routes — events, calendars, sync

**Files:**

- Modify: `src/api/routes/calendar.py`
- Test: `tests/test_api_calendar.py`

**Interfaces:**

- Consumes: `CalendarReader` (live reads, offloaded via executor), `CalendarSyncJob`.
- Produces (extends existing `init(repo)`):
  - `def init(repo, reader=None, sync_job=None) -> None`
  - `GET /api/calendar/events?start=&end=` → `{"events": [CalendarEvent.to_dict()...], "count": n}`
  - `GET /api/calendar/calendars` → `{"calendars": [{id, title}...]}`
  - `POST /api/calendar/sync` → `{"synced": n}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_calendar.py`:

```python
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import calendar as calendar_routes
from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository
from src.calendar_events.sync import CalendarSyncJob
from src.db.database import Database
from src.db.repository import MeetingRepository

TEST_TOKEN = "test-token-for-calendar"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


class FakeReader:
    def __init__(self, events=None, calendars=None, available=True):
        self._events = events or []
        self._calendars = calendars or []
        self.available = available

    def list_events(self, start, end, excluded_calendars=None):
        return [e for e in self._events if start <= e.start_ts < end]

    def list_calendars(self):
        return self._calendars


def _ev(uid="EK1:1000", start=1000.0):
    return CalendarEvent(
        event_uid=uid, title="Sync", start_ts=start, end_ts=start + 1800.0,
        attendees=[{"name": "A"}, {"name": "B"}], organizer=None,
        join_url="", meeting_id="", calendar_name="Work",
    )


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "cal_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    cal_repo = CalendarEventRepository(db)
    reader = FakeReader(events=[_ev()], calendars=[{"id": "c1", "title": "Work"}])
    sync_job = CalendarSyncJob(cal_repo)
    calendar_routes.init(repo, reader, sync_job)
    app = FastAPI()
    app.include_router(calendar_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "cal_repo": cal_repo, "reader": reader}
    await db.close()


@pytest.mark.asyncio
async def test_get_events_returns_events_in_range(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["events"][0]["event_uid"] == "EK1:1000"


@pytest.mark.asyncio
async def test_get_events_rejects_bad_range(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=5000&end=1000", headers=_auth_headers())
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_calendars(api):
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/calendars", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["calendars"] == [{"id": "c1", "title": "Work"}]


@pytest.mark.asyncio
async def test_post_sync_mirrors_into_table(api):
    with TestClient(api["app"]) as c:
        r = c.post("/api/calendar/sync", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json()["synced"] == 1
    rows = await api["cal_repo"].list_by_range(0.0, 10 ** 12)
    assert [row["event_uid"] for row in rows] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_events_empty_when_reader_unavailable(api):
    api["reader"].available = False
    with TestClient(api["app"]) as c:
        r = c.get("/api/calendar/events?start=0&end=5000", headers=_auth_headers())
        assert r.status_code == 200
        assert r.json() == {"events": [], "count": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_calendar.py -v`
Expected: FAIL — `init()` takes 1 arg / routes `/api/calendar/events` etc. do not exist (404).

- [ ] **Step 3: Write minimal implementation**

Replace `src/api/routes/calendar.py` with:

```python
"""Calendar endpoints: recorded-meeting range + upcoming-event import (Track B)."""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Query

from src.utils.config import load_config

logger = logging.getLogger("contextrecall.api.calendar")

router = APIRouter()

# Injected at startup.
_repo = None          # MeetingRepository
_reader = None        # CalendarReader | None
_sync_job = None      # CalendarSyncJob | None


def init(repo, reader=None, sync_job=None) -> None:
    global _repo, _reader, _sync_job
    _repo = repo
    _reader = reader
    _sync_job = sync_job


_MAX_RANGE_SECONDS = 366 * 86400  # ~1 year


def _validate_range(start: float, end: float) -> None:
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    if (end - start) > _MAX_RANGE_SECONDS:
        raise HTTPException(status_code=422, detail="range must not exceed 366 days")


@router.get("/api/calendar/meetings", summary="List meetings for calendar view")
async def get_calendar_meetings(
    start: float = Query(..., description="Start unix timestamp (inclusive)"),
    end: float = Query(..., description="End unix timestamp (exclusive)"),
):
    """Return all meetings whose started_at falls within [start, end)."""
    _validate_range(start, end)
    meetings = await _repo.list_meetings_by_date_range(start, end)
    return {"meetings": [m.to_dict() for m in meetings], "count": len(meetings)}


@router.get("/api/calendar/events", summary="List upcoming calendar events")
async def get_calendar_events(
    start: float = Query(..., description="Start unix timestamp (inclusive)"),
    end: float = Query(..., description="End unix timestamp (exclusive)"),
):
    """Return meeting-like calendar events in [start, end), read live from EventKit."""
    _validate_range(start, end)
    if _reader is None or not getattr(_reader, "available", False):
        return {"events": [], "count": 0}
    excluded = load_config().calendar.excluded_calendars
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _reader.list_events, start, end, excluded)
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@router.get("/api/calendar/calendars", summary="List available calendars")
async def get_calendars():
    """Return [{id, title}] for the Settings calendar-exclude UI."""
    if _reader is None or not getattr(_reader, "available", False):
        return {"calendars": []}
    loop = asyncio.get_running_loop()
    calendars = await loop.run_in_executor(None, _reader.list_calendars)
    return {"calendars": calendars}


@router.post("/api/calendar/sync", summary="Sync the calendar mirror now")
async def sync_calendar():
    """Mirror the rolling near-term window into calendar_events immediately."""
    if _reader is None or _sync_job is None or not getattr(_reader, "available", False):
        return {"synced": 0}
    config = load_config().calendar
    now = time.time()
    end = now + config.sync_horizon_days * 86400
    excluded = config.excluded_calendars
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, _reader.list_events, now, end, excluded)
    synced = await _sync_job.apply(now, end, events)
    return {"synced": synced}
```

Note: `run_in_executor(None, _reader.list_events, start, end, excluded)` passes positional args; `list_events(self, start, end, excluded_calendars=None)` accepts them positionally.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api_calendar.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/calendar.py tests/test_api_calendar.py
git commit -m "feat(api): calendar events/calendars/sync endpoints"
```

---

### Task 7: Server wiring — reader construction + route init + scheduler job

**Files:**

- Modify: `src/api/server.py` (construct reader/sync_job, extend `calendar_routes.init(...)`, register `calendar_sync` job, add `_sync_calendar`)
- Test: `tests/test_server_calendar_sync.py`

**Interfaces:**

- Consumes: `CalendarReader`, `CalendarEventRepository`, `CalendarSyncJob`, `config.calendar`.
- Produces: at startup, `self._calendar_reader` and `self._calendar_sync` are set (reader gated on `enabled or import_enabled`); `calendar_routes.init(self.repo, self._calendar_reader, self._calendar_sync)`; a `"calendar_sync"` scheduler job registered when `config.calendar.import_enabled`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_server_calendar_sync.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.api.server import ApiServer


class _RecordingScheduler:
    def __init__(self):
        self.registered = []

    def register(self, name, func, interval):
        self.registered.append((name, interval))


def _config(import_enabled=True):
    # Minimal config object with just the attributes _setup_scheduler_jobs touches.
    return SimpleNamespace(
        notifications=SimpleNamespace(enabled=False),
        analytics=SimpleNamespace(refresh_interval_hours=6),
        series=SimpleNamespace(heuristic_enabled=False),
        calendar=SimpleNamespace(import_enabled=import_enabled, sync_interval_minutes=15),
    )


def test_calendar_sync_job_registered_when_import_enabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(import_enabled=True)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "calendar_sync" in names
    interval = dict(server._scheduler.registered)["calendar_sync"]
    assert interval == 15 * 60


def test_calendar_sync_job_absent_when_import_disabled():
    server = ApiServer()
    server._scheduler = _RecordingScheduler()
    with patch("src.api.server.load_config", return_value=_config(import_enabled=False)):
        server._setup_scheduler_jobs()
    names = [n for n, _ in server._scheduler.registered]
    assert "calendar_sync" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_server_calendar_sync.py -v`
Expected: FAIL — no `"calendar_sync"` job is registered.

- [ ] **Step 3: Write minimal implementation**

In `src/api/server.py`:

(a) In `_setup_scheduler_jobs`, after the existing `series_detect` registration block, add:

```python
        if config.calendar.import_enabled:
            self._scheduler.register(
                "calendar_sync",
                lambda: safe_run("calendar_sync", self._sync_calendar),
                config.calendar.sync_interval_minutes * 60,
            )
```

(b) Add the job method (near `_run_series_detection`):

```python
    async def _sync_calendar(self) -> None:
        """Mirror the rolling calendar window into calendar_events."""
        import time

        reader = getattr(self, "_calendar_reader", None)
        sync = getattr(self, "_calendar_sync", None)
        if reader is None or sync is None or not reader.available:
            return
        config = load_config().calendar
        now = time.time()
        end = now + config.sync_horizon_days * 86400
        excluded = config.excluded_calendars
        loop = asyncio.get_running_loop()
        events = await loop.run_in_executor(None, reader.list_events, now, end, excluded)
        await sync.apply(now, end, events)
```

(c) Where the other routers are initialised in `_create_app` (the block with `series_routes.init(...)` / `prep_routes.init(...)`), construct the reader/sync and wire the calendar route. Find the existing `calendar_routes.init(self.repo)` call (or the calendar router init) and replace it with:

```python
        from src.calendar_events.reader import CalendarReader
        from src.calendar_events.repository import CalendarEventRepository
        from src.calendar_events.sync import CalendarSyncJob

        _cal_cfg = load_config().calendar
        if _cal_cfg.enabled or _cal_cfg.import_enabled:
            self._calendar_reader = CalendarReader(
                excluded_calendars=_cal_cfg.excluded_calendars
            )
        else:
            self._calendar_reader = None
        self._calendar_sync = CalendarSyncJob(CalendarEventRepository(self.db))
        calendar_routes.init(self.repo, self._calendar_reader, self._calendar_sync)
```

(If `calendar_routes` is not yet imported at the top of `server.py`, add `from src.api.routes import calendar as calendar_routes` alongside the other route imports, and ensure `app.include_router(calendar_routes.router, dependencies=auth_deps)` remains.)

(d) Declare the attributes in `__init__` (near `self.repo: MeetingRepository | None = None`):

```python
        self._calendar_reader = None
        self._calendar_sync = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_server_calendar_sync.py -v`
Expected: PASS (both tests).

Then run the broader server/API suite to catch wiring regressions:
Run: `python3 -m pytest tests/test_api_calendar.py tests/test_server_calendar_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/server.py tests/test_server_calendar_sync.py
git commit -m "feat(api): wire calendar reader + sync scheduler job"
```

---

### Task 8: UI types — `CalendarEvent`, `CalendarConfig`

**Files:**

- Modify: `ui/src/lib/types.ts`
- Test: (type-only — verified by `npx tsc --noEmit`)

**Interfaces:**

- Produces:
  - `interface CalendarEvent { event_uid; title; start_ts; end_ts; attendees; organizer; join_url; meeting_id; calendar_name }`
  - `interface CalendarEventsResponse { events: CalendarEvent[]; count: number }`
  - `interface CalendarConfig { ... }` and `calendar: CalendarConfig` on `AppConfig`.

- [ ] **Step 1: Add the types**

In `ui/src/lib/types.ts`, add:

```typescript
export interface CalendarAttendee {
  name: string;
  email: string;
}

export interface CalendarEvent {
  event_uid: string;
  title: string;
  start_ts: number;
  end_ts: number;
  attendees: CalendarAttendee[];
  organizer: CalendarAttendee | null;
  join_url: string;
  meeting_id: string;
  calendar_name: string;
}

export interface CalendarEventsResponse {
  events: CalendarEvent[];
  count: number;
}

export interface CalendarConfig {
  enabled: boolean;
  time_window_minutes: number;
  min_confidence: number;
  import_enabled: boolean;
  sync_interval_minutes: number;
  sync_horizon_days: number;
  excluded_calendars: string[];
}
```

Add `calendar` to the `AppConfig` interface:

```typescript
export interface AppConfig {
  detection: DetectionConfig;
  audio: AudioConfig;
  transcription: TranscriptionConfig;
  summarisation: SummarisationConfig;
  diarisation: DiarisationConfig;
  markdown: MarkdownConfig;
  notion: NotionConfig;
  logging: LoggingConfig;
  api: ApiConfig;
  calendar: CalendarConfig;
  retention: RetentionConfig;
  notifications: NotificationsConfig;
}
```

- [ ] **Step 2: Verify types compile**

Run: `cd ui && npx tsc --noEmit`
Expected: PASS (no type errors). (`calendar` is optional in practice because the backend always returns it; existing config consumers are unaffected.)

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/types.ts
git commit -m "feat(ui): CalendarEvent + CalendarConfig types"
```

---

### Task 9: UI API client — events, calendars, sync

**Files:**

- Modify: `ui/src/lib/api.ts`
- Test: `ui/src/lib/__tests__/api.test.ts`

**Interfaces:**

- Consumes: `CalendarEvent`, `CalendarEventsResponse` types.
- Produces:
  - `getCalendarEvents(start: number, end: number): Promise<CalendarEventsResponse>`
  - `getCalendars(): Promise<{ calendars: { id: string; title: string }[] }>`
  - `triggerCalendarSync(): Promise<{ synced: number }>`

- [ ] **Step 1: Write the failing test**

Add to `ui/src/lib/__tests__/api.test.ts` (add `getCalendarEvents`, `getCalendars`, `triggerCalendarSync` to the import from `../api`):

```typescript
describe("calendar import", () => {
  it("getCalendarEvents requests the range", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ events: [], count: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    await getCalendarEvents(100, 200);
    expect(calls[0]).toContain("/api/calendar/events?start=100&end=200");
  });

  it("triggerCalendarSync POSTs", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(JSON.stringify({ synced: 3 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;

    const res = await triggerCalendarSync();
    expect(res.synced).toBe(3);
    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/calendar/sync");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- api.test`
Expected: FAIL — `getCalendarEvents`/`triggerCalendarSync` not exported.

- [ ] **Step 3: Write minimal implementation**

In `ui/src/lib/api.ts`, add `CalendarEventsResponse` to the `import type { ... } from "./types"` block, then add near `getCalendarMeetings`:

```typescript
export async function getCalendarEvents(
  start: number,
  end: number,
): Promise<CalendarEventsResponse> {
  return request<CalendarEventsResponse>(
    `/api/calendar/events?start=${start}&end=${end}`,
  );
}

export async function getCalendars(): Promise<{
  calendars: { id: string; title: string }[];
}> {
  return request<{ calendars: { id: string; title: string }[] }>(
    "/api/calendar/calendars",
  );
}

export async function triggerCalendarSync(): Promise<{ synced: number }> {
  return request<{ synced: number }>("/api/calendar/sync", { method: "POST" });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- api.test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): calendar events/calendars/sync API client"
```

---

### Task 10: `UpcomingEventCard` component

**Files:**

- Create: `ui/src/components/calendar/UpcomingEventCard.tsx`
- Test: `ui/src/components/calendar/__tests__/UpcomingEventCard.test.tsx`

**Interfaces:**

- Consumes: `CalendarEvent`.
- Produces: `function UpcomingEventCard({ event, compact }: { event: CalendarEvent; compact?: boolean })` — renders the event distinctly (dashed/muted "scheduled" style); clicking toggles an inline detail popover (title, time, attendees, join link). No router navigation (the event is not a recorded meeting).

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/UpcomingEventCard.test.tsx`:

```typescript
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Design sync",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [{ name: "Alice", email: "a@x.com" }, { name: "Bob", email: "b@x.com" }],
  organizer: null,
  join_url: "https://teams.microsoft.com/l/meetup-join/x",
  meeting_id: "19:abc",
  calendar_name: "Work",
};

describe("UpcomingEventCard", () => {
  it("renders the event title", () => {
    render(<UpcomingEventCard event={EVENT} />);
    expect(screen.getByText("Design sync")).toBeInTheDocument();
  });

  it("reveals attendees in a popover on click", () => {
    render(<UpcomingEventCard event={EVENT} />);
    fireEvent.click(screen.getByRole("button", { name: /Design sync/i }));
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText(/Join/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- UpcomingEventCard`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `ui/src/components/calendar/UpcomingEventCard.tsx`:

```typescript
import { useState } from "react";
import { format } from "date-fns";
import type { CalendarEvent } from "../../lib/types";

interface UpcomingEventCardProps {
  event: CalendarEvent;
  compact?: boolean;
}

/** Renders an imported (not-yet-recorded) calendar event, distinct from recorded meetings. */
export function UpcomingEventCard({ event, compact = false }: UpcomingEventCardProps) {
  const [open, setOpen] = useState(false);
  const title = event.title || "Untitled";
  const start = format(new Date(event.start_ts * 1000), "HH:mm");

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
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- UpcomingEventCard`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/UpcomingEventCard.tsx ui/src/components/calendar/__tests__/UpcomingEventCard.test.tsx
git commit -m "feat(ui): UpcomingEventCard with detail popover"
```

---

### Task 11: Grid components `events` prop + CalendarView threading

**Files:**

- Modify: `ui/src/components/calendar/MonthGrid.tsx`, `WeekTimeline.tsx`, `DayDetail.tsx`, `AgendaList.tsx`, `CalendarView.tsx`
- Test: `ui/src/components/calendar/__tests__/CalendarEvents.test.tsx`

**Interfaces:**

- Consumes: `CalendarEvent`, `UpcomingEventCard`, `getCalendarEvents`.
- Produces: each grid gains an optional `events?: CalendarEvent[]` prop; `CalendarView` runs a parallel events query and threads `events` into each view.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/calendar/__tests__/CalendarEvents.test.tsx`:

```typescript
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AgendaList } from "../AgendaList";
import type { CalendarEvent } from "../../../lib/types";

const EVENT: CalendarEvent = {
  event_uid: "EK1:1700000000",
  title: "Upcoming standup",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [{ name: "Alice", email: "a@x.com" }, { name: "Bob", email: "b@x.com" }],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("AgendaList with events", () => {
  it("renders upcoming events alongside meetings", () => {
    render(
      <MemoryRouter>
        <AgendaList meetings={[]} events={[EVENT]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Upcoming standup")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- CalendarEvents`
Expected: FAIL — `AgendaList` does not accept an `events` prop (type error / not rendered).

- [ ] **Step 3: Write minimal implementation**

For each grid, add the optional prop and render events through `UpcomingEventCard`, keyed by `event.event_uid`, grouped/filtered by the same day logic already present.

`AgendaList.tsx` — extend props and merge events into the per-day groups:

```typescript
import { UpcomingEventCard } from "./UpcomingEventCard";
import type { CalendarEvent } from "../../lib/types";

interface AgendaListProps {
  meetings: Meeting[];
  events?: CalendarEvent[];
}

export function AgendaList({ meetings, events = [] }: AgendaListProps) {
  // ...existing meeting grouping unchanged...
```

After the existing `groups` are built for meetings, add an events section inside each day group's render (simplest: render an events block per group date). Add a helper that groups events by day key and render them under the matching header. Minimal approach — append an events list to each rendered group:

```typescript
const eventsByDay = new Map<string, CalendarEvent[]>();
for (const ev of events) {
  const key = format(new Date(ev.start_ts * 1000), "yyyy-MM-dd");
  const list = eventsByDay.get(key) ?? [];
  list.push(ev);
  eventsByDay.set(key, list);
}
// Ensure days that ONLY have events still get a group:
for (const key of eventsByDay.keys()) {
  if (!groups.some((g) => g.date === key)) {
    groups.push({ date: key, meetings: [] });
  }
}
groups.sort((a, b) => (a.date < b.date ? 1 : -1)); // keep newest-first
```

Then inside each group's JSX (after the meetings map), render its events:

```tsx
{
  (eventsByDay.get(group.date) ?? [])
    .sort((a, b) => a.start_ts - b.start_ts)
    .map((ev) => (
      <div key={ev.event_uid} className="flex items-start gap-2">
        <span className="text-[11px] text-text-muted w-12 pt-1 text-right shrink-0">
          {format(new Date(ev.start_ts * 1000), "HH:mm")}
        </span>
        <div className="flex-1">
          <UpcomingEventCard event={ev} />
        </div>
      </div>
    ));
}
```

`MonthGrid.tsx` — add `events?: CalendarEvent[]` to `MonthGridProps` (default `[]`), build `eventsByDay` with the same `yyyy-MM-dd` key, and inside the day cell after the meetings `.slice(0,3)` block render up to a couple of events:

```tsx
{
  (eventsByDay.get(key) ?? [])
    .slice(0, 2)
    .map((ev) => <UpcomingEventCard key={ev.event_uid} event={ev} compact />);
}
```

`DayDetail.tsx` — add `events?: CalendarEvent[]`, filter to `currentDate` via `isSameDay(new Date(ev.start_ts * 1000), currentDate)`, and render each through `UpcomingEventCard` in the same timeline column layout (a row per event, keyed by `event_uid`).

`WeekTimeline.tsx` — add `events?: CalendarEvent[]`; filter per day with `isSameDay(new Date(ev.start_ts * 1000), day)` and render each as an absolutely-positioned block using the SAME clamp math as meetings (START_HOUR=7, END_HOUR=22, HOUR_HEIGHT=48), but styled dashed/muted and keyed by `event_uid`. Position from `getHours(new Date(ev.start_ts*1000)) + getMinutes(...)/60`; height from `(ev.end_ts - ev.start_ts)/3600`.

`CalendarView.tsx` — add the parallel query and thread `events`:

```tsx
const { data: eventsData } = useQuery({
  queryKey: ["calendar-events", start, end],
  queryFn: () => getCalendarEvents(start, end),
  enabled: daemonRunning,
  staleTime: 30_000,
});
const events = eventsData?.events ?? [];
```

(import `getCalendarEvents` from `../../lib/api`) and pass `events={events}` to each view:

```tsx
{
  viewMode === "month" && (
    <MonthGrid
      currentDate={currentDate}
      meetings={meetings}
      events={events}
      onDayClick={handleDayClick}
    />
  );
}
{
  viewMode === "week" && (
    <WeekTimeline
      currentDate={currentDate}
      meetings={meetings}
      events={events}
    />
  );
}
{
  viewMode === "day" && (
    <DayDetail currentDate={currentDate} meetings={meetings} events={events} />
  );
}
{
  viewMode === "agenda" && <AgendaList meetings={meetings} events={events} />;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- CalendarEvents`
Expected: PASS.
Then: `cd ui && npx tsc --noEmit` — Expected: PASS (all grids accept `events`).
Then: `cd ui && npm test` — Expected: existing calendar tests still PASS (the `events` prop is optional/defaulted).

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/calendar/
git commit -m "feat(ui): render upcoming events in calendar grids"
```

---

### Task 12: `CalendarsSection` settings + registration

**Files:**

- Create: `ui/src/components/settings/CalendarsSection.tsx`
- Modify: `ui/src/components/settings/Settings.tsx` (import, `SETTINGS_SECTIONS`, mount)
- Test: `ui/src/components/settings/__tests__/CalendarsSection.test.tsx`

**Interfaces:**

- Consumes: `getCalendars()`, `getConfig()`, `updateConfig()`, `triggerCalendarSync()`.
- Produces: `function CalendarsSection({ id }: { id?: string })` — lists available calendars with an include/exclude toggle each (persisting `calendar.excluded_calendars` via `updateConfig`), plus a "Sync now" button.

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/settings/__tests__/CalendarsSection.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { CalendarsSection } from "../CalendarsSection";
import { ToastProvider } from "../../common/Toast";

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

describe("CalendarsSection", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/calendar/calendars")) {
        return new Response(
          JSON.stringify({ calendars: [{ id: "c1", title: "Work" }, { id: "c2", title: "Personal" }] }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/config")) {
        return new Response(
          JSON.stringify({ calendar: { excluded_calendars: ["Personal"] } }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
  });

  it("lists available calendars", async () => {
    render(<CalendarsSection id="calendars" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Work")).toBeInTheDocument());
    expect(screen.getByText("Personal")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- CalendarsSection`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `ui/src/components/settings/CalendarsSection.tsx`:

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getCalendars,
  getConfig,
  updateConfig,
  triggerCalendarSync,
} from "../../lib/api";
import { useToast } from "../common/Toast";

/** Settings panel: choose which calendars to import, and sync now. */
export function CalendarsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: calData } = useQuery({
    queryKey: ["calendars"],
    queryFn: getCalendars,
  });
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: getConfig });

  const excluded = config?.calendar?.excluded_calendars ?? [];
  const calendars = calData?.calendars ?? [];

  const save = useMutation({
    mutationFn: (next: string[]) =>
      updateConfig({ calendar: { excluded_calendars: next } } as never),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Calendar selection saved.");
    },
    onError: () => toast.error("Failed to save calendar selection."),
  });

  const syncNow = useMutation({
    mutationFn: triggerCalendarSync,
    onSuccess: (r) => toast.success(`Synced ${r.synced} events.`),
    onError: () => toast.error("Sync failed."),
  });

  function toggle(title: string, include: boolean) {
    const next = include
      ? excluded.filter((t) => t !== title)
      : [...excluded, title];
    save.mutate(next);
  }

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Calendars</legend>
      <h2 className="text-sm font-medium text-text-primary">Calendars</h2>
      <p className="text-xs text-text-muted mt-1">
        Choose which calendars to import upcoming meetings from.
      </p>

      <div className="py-3 flex flex-col gap-2">
        {calendars.length === 0 ? (
          <p className="text-sm text-text-muted">No calendars available.</p>
        ) : (
          calendars.map((c) => {
            const included = !excluded.includes(c.title);
            return (
              <label key={c.id} className="flex items-center gap-2 text-sm text-text-secondary">
                <input
                  type="checkbox"
                  checked={included}
                  onChange={(e) => toggle(c.title, e.target.checked)}
                />
                {c.title}
              </label>
            );
          })
        )}
      </div>

      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending}
        className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
      >
        Sync now
      </button>
    </fieldset>
  );
}
```

In `ui/src/components/settings/Settings.tsx`:

- Import: `import { CalendarsSection } from "./CalendarsSection";`
- Add to `SETTINGS_SECTIONS` (after `{ id: "notion", label: "Notion" }`): `{ id: "calendars", label: "Calendars" },`
- Mount it alongside the other daemon-gated sections: `{daemonRunning && <CalendarsSection id="calendars" />}`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- CalendarsSection`
Expected: PASS.
Then: `cd ui && npx tsc --noEmit` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/settings/CalendarsSection.tsx ui/src/components/settings/Settings.tsx ui/src/components/settings/__tests__/CalendarsSection.test.tsx
git commit -m "feat(ui): Calendars settings section (exclude + sync now)"
```

---

### Task 13: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Python suite + lint**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all, including the new calendar tests). If any pre-existing unrelated test fails, note it but do not fix in this task.
Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 2: UI suite + types**

Run: `cd ui && npm test`
Expected: PASS.
Run: `cd ui && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 3: Commit (if any lint/format fixups were needed)**

```bash
git add -A
git commit -m "chore(calendar): lint + test fixups"
```

(Skip if nothing changed.)

---

## Self-Review

**Spec coverage:**

- Import meeting-like events + agenda merge → Tasks 3, 10, 11. ✔
- Store rolling window (calendar_events v18) → Tasks 1, 4, 5. ✔
- Live UI reads + background mirror (Hybrid) → Tasks 6 (`/events` live), 5+7 (mirror job). ✔
- Settings exclude-list + all-calendars default → Tasks 2 (config), 12 (UI). ✔
- Recurring `event_uid = identifier:start_ts` → Task 3 (`_events_from_extracted`). ✔
- Reader reuses matcher helpers, matcher untouched → Task 3. ✔
- Off-loop EventKit reads → Tasks 6, 7 (`run_in_executor`). ✔
- Prune skips `recorded_meeting_id` rows → Tasks 4, 5. ✔
- Degrade-to-empty when EventKit unavailable → Tasks 3, 6. ✔
- `enabled OR import_enabled` init gate → Task 7. ✔
- Config example updated → Task 2. ✔

**Deliberate deviation from spec (flag at plan review):** the spec said `CalendarMatcher` would "delegate EventKit access to the reader." The plan instead makes `CalendarReader` a sibling that reuses the matcher's _pure_ helpers, leaving `CalendarMatcher.match()` untouched — because full delegation would change the matcher's time-window semantics (it currently matches on non-meeting-like events too). Net effect still satisfies "no behaviour change to the reactive path" and "one definition of the parsing helpers."

**Placeholder scan:** none — every step carries concrete code/commands.

**Type consistency:** `list_events(self, start, end, excluded_calendars=None)` used identically in reader, routes (Task 6), and server (Task 7). `CalendarEvent.to_dict()` used by routes. `CalendarSyncJob(repo).apply(start, end, events)` used in Tasks 5, 6, 7. `CalendarEventRepository(db)` constructor consistent across Tasks 4–7. TS `CalendarEvent`/`CalendarEventsResponse` consistent across Tasks 8, 9, 10, 11.
