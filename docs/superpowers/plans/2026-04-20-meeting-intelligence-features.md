# Meeting Intelligence Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add recurring meeting detection, analytics insights, action item tracking, meeting prep mode, and smart notifications to MeetingMind.

**Architecture:** Extends existing pipeline with post-processing hooks, adds a lightweight asyncio scheduler for time-based triggers, and introduces a multi-channel notification dispatcher. All features share a single schema migration (v8 to v9) and degrade gracefully when dependencies are unavailable.

**Tech Stack:** Python 3.10+ / FastAPI / aiosqlite / React 18 / TypeScript / TailwindCSS / Tauri

---

## Phase 1: Foundation (Schema, Config, Shared Infrastructure)

### Task 1: Database Migration v9

**Files:**

- Modify: `src/db/database.py`
- Test: `tests/test_db_migration_v9.py`

- [ ] **Step 1: Write the migration test**

```python
# tests/test_db_migration_v9.py
"""Tests for schema migration to version 9."""

import pytest
from src.db.database import Database, SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migration_creates_new_tables(tmp_path):
    """Fresh DB at v9 should have all new tables."""
    db = Database(db_path=tmp_path / "test.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        for table in ("meeting_series", "action_items", "meeting_analytics",
                      "notifications", "prep_briefings"):
            cursor = await db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            row = await cursor.fetchone()
            assert row is not None, f"Table {table} should exist"

        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "series_id" in columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v8(tmp_path):
    """Existing v8 DB should migrate to v9 without data loss."""
    import aiosqlite

    db_path = tmp_path / "existing.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA user_version = 8")
        await conn.execute("""
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Untitled',
                started_at REAL NOT NULL,
                ended_at REAL,
                duration_seconds REAL,
                status TEXT NOT NULL DEFAULT 'recording',
                audio_path TEXT,
                transcript_json TEXT,
                summary_markdown TEXT,
                tags TEXT,
                language TEXT,
                word_count INTEGER,
                label TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                calendar_event_title TEXT DEFAULT '',
                attendees_json TEXT DEFAULT '[]',
                calendar_confidence REAL DEFAULT 0.0,
                teams_join_url TEXT DEFAULT '',
                teams_meeting_id TEXT DEFAULT ''
            )
        """)
        await conn.execute(
            "INSERT INTO meetings (id, title, started_at, status, created_at, updated_at) "
            "VALUES ('test-1', 'Old Meeting', 1700000000, 'complete', 1700000000, 1700000000)"
        )
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == 9

        cursor = await db.conn.execute("SELECT title FROM meetings WHERE id='test-1'")
        row = await cursor.fetchone()
        assert row[0] == "Old Meeting"

        cursor = await db.conn.execute("SELECT series_id FROM meetings WHERE id='test-1'")
        row = await cursor.fetchone()
        assert row[0] is None
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v9.py -v`
Expected: FAIL (SCHEMA_VERSION is 8, new tables don't exist)

- [ ] **Step 3: Implement the migration**

In `src/db/database.py`:

1. Change `SCHEMA_VERSION = 8` to `SCHEMA_VERSION = 9`
2. Add new table SQL constants after `SPEAKER_MAPPINGS_SQL`:

```python
MEETING_SERIES_SQL = """
CREATE TABLE IF NOT EXISTS meeting_series (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    calendar_series_id TEXT,
    detection_method TEXT NOT NULL,
    typical_attendees_json TEXT,
    typical_day_of_week INTEGER,
    typical_time TEXT,
    typical_duration_minutes INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meeting_series_calendar
    ON meeting_series(calendar_series_id);
"""

ACTION_ITEMS_SQL = """
CREATE TABLE IF NOT EXISTS action_items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    assignee TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT DEFAULT 'medium',
    due_date TEXT,
    reminder_at TEXT,
    source TEXT NOT NULL DEFAULT 'extracted',
    extracted_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_action_items_meeting ON action_items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_action_items_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_action_items_assignee ON action_items(assignee);
CREATE INDEX IF NOT EXISTS idx_action_items_due_date ON action_items(due_date);
"""

MEETING_ANALYTICS_SQL = """
CREATE TABLE IF NOT EXISTS meeting_analytics (
    id TEXT PRIMARY KEY,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    total_meetings INTEGER DEFAULT 0,
    total_duration_minutes INTEGER DEFAULT 0,
    total_words INTEGER DEFAULT 0,
    unique_attendees INTEGER DEFAULT 0,
    recurring_ratio REAL DEFAULT 0,
    action_items_created INTEGER DEFAULT 0,
    action_items_completed INTEGER DEFAULT 0,
    busiest_hour INTEGER,
    computed_at TEXT NOT NULL,
    UNIQUE(period_type, period_start)
);
"""

NOTIFICATIONS_SQL = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    reference_id TEXT,
    channel TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    scheduled_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications(type);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notifications_reference ON notifications(reference_id);
"""

PREP_BRIEFINGS_SQL = """
CREATE TABLE IF NOT EXISTS prep_briefings (
    id TEXT PRIMARY KEY,
    meeting_id TEXT,
    series_id TEXT,
    content_markdown TEXT NOT NULL,
    attendees_json TEXT,
    related_meeting_ids_json TEXT,
    open_action_items_json TEXT,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (series_id) REFERENCES meeting_series(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_prep_briefings_series ON prep_briefings(series_id);
CREATE INDEX IF NOT EXISTS idx_prep_briefings_expires ON prep_briefings(expires_at);
"""
```

3. Update `_ALLOWED_TABLES`:

```python
_ALLOWED_TABLES = frozenset({
    "meetings", "speaker_mappings", "segment_embeddings",
    "meeting_series", "action_items", "meeting_analytics",
    "notifications", "prep_briefings",
})
```

4. In `_migrate()`, add after the `if current_version < 8:` block (before `else:`):

```python
        if current_version < 9:
            await self.conn.executescript(MEETING_SERIES_SQL)
            await self.conn.executescript(ACTION_ITEMS_SQL)
            await self.conn.executescript(MEETING_ANALYTICS_SQL)
            await self.conn.executescript(NOTIFICATIONS_SQL)
            await self.conn.executescript(PREP_BRIEFINGS_SQL)
            await _safe_add_column(self.conn, "meetings", "series_id", "TEXT", "NULL")
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 9 (meeting intelligence)")
```

5. In the fresh-install block (`if current_version < 1:`), add after `SPEAKER_MAPPINGS_SQL`:

```python
            await self.conn.executescript(MEETING_SERIES_SQL)
            await self.conn.executescript(ACTION_ITEMS_SQL)
            await self.conn.executescript(MEETING_ANALYTICS_SQL)
            await self.conn.executescript(NOTIFICATIONS_SQL)
            await self.conn.executescript(PREP_BRIEFINGS_SQL)
            await _safe_add_column(self.conn, "meetings", "series_id", "TEXT", "NULL")
```

6. In `src/db/repository.py`, add `"series_id"` to `_MUTABLE_COLUMNS` and add `series_id: str | None = None` field to `MeetingRecord`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db_migration_v9.py -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -x`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/db/database.py src/db/repository.py tests/test_db_migration_v9.py
git commit -m "feat: add schema v9 migration for meeting intelligence tables"
```

---

### Task 2: Configuration Dataclasses

**Files:**

- Modify: `src/utils/config.py`
- Test: `tests/test_config_intelligence.py`

- [ ] **Step 1: Write the config test**

```python
# tests/test_config_intelligence.py
"""Tests for new intelligence config sections."""

import pytest
import yaml
from pathlib import Path
from src.utils.config import (
    load_config,
    AppConfig,
    ActionItemsConfig,
    SeriesConfig,
    AnalyticsConfig,
    NotificationsConfig,
    PrepConfig,
)


def test_new_config_sections_have_defaults():
    config = AppConfig()
    assert config.action_items.auto_extract is True
    assert config.series.min_meetings_for_series == 3
    assert config.analytics.refresh_interval_hours == 6
    assert config.notifications.enabled is True
    assert config.prep.lead_time_minutes == 15


def test_new_config_sections_load_from_yaml(tmp_path):
    config_data = {
        "action_items": {"auto_extract": False, "duplicate_threshold": 0.9},
        "series": {"heuristic_enabled": False},
        "analytics": {"health_alert_threshold": 2.0},
        "notifications": {"enabled": False},
        "prep": {"lead_time_minutes": 30, "max_context_meetings": 5},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))
    config = load_config(path)
    assert config.action_items.auto_extract is False
    assert config.action_items.duplicate_threshold == 0.9
    assert config.series.heuristic_enabled is False
    assert config.analytics.health_alert_threshold == 2.0
    assert config.notifications.enabled is False
    assert config.prep.lead_time_minutes == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_intelligence.py -v`
Expected: FAIL

- [ ] **Step 3: Add dataclasses to `src/utils/config.py`**

Add before `AppConfig`:

```python
@dataclass
class ActionItemsConfig:
    auto_extract: bool = True
    default_reminder_before_due: str = "1d"
    duplicate_threshold: float = 0.85


@dataclass
class SeriesConfig:
    heuristic_enabled: bool = True
    min_meetings_for_series: int = 3
    attendee_overlap_threshold: float = 0.6
    title_similarity_threshold: float = 0.7
    time_tolerance_hours: int = 1
    day_tolerance: int = 1


@dataclass
class AnalyticsConfig:
    refresh_interval_hours: int = 6
    rolling_window_weeks: int = 4
    health_alert_threshold: float = 1.5


@dataclass
class WebhookChannelConfig:
    enabled: bool = False
    url: str = ""
    format: str = "slack"


@dataclass
class EmailChannelConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    to_address: str = ""
    max_per_day: int = 3


@dataclass
class NotificationsConfig:
    enabled: bool = True
    in_app: bool = True
    macos: bool = True
    webhook: WebhookChannelConfig = field(default_factory=WebhookChannelConfig)
    email: EmailChannelConfig = field(default_factory=EmailChannelConfig)
    default_reminder_before_due: str = "1d"
    overdue_check_interval: str = "6h"


@dataclass
class PrepConfig:
    lead_time_minutes: int = 15
    auto_generate: bool = True
    max_context_meetings: int = 3
    max_attendee_history: int = 5
    briefing_ttl_hours: int = 2
```

Add fields to `AppConfig`:

```python
    action_items: ActionItemsConfig = field(default_factory=ActionItemsConfig)
    series: SeriesConfig = field(default_factory=SeriesConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    prep: PrepConfig = field(default_factory=PrepConfig)
```

Add to `load_config()`:

```python
        action_items=_build_dataclass(ActionItemsConfig, raw.get("action_items", {})),
        series=_build_dataclass(SeriesConfig, raw.get("series", {})),
        analytics=_build_dataclass(AnalyticsConfig, raw.get("analytics", {})),
        notifications=_build_dataclass(NotificationsConfig, raw.get("notifications", {})),
        prep=_build_dataclass(PrepConfig, raw.get("prep", {})),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_intelligence.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/utils/config.py tests/test_config_intelligence.py
git commit -m "feat: add config dataclasses for intelligence features"
```

---

### Task 3: Notification Dispatcher

**Files:**

- Create: `src/notifications/__init__.py`, `src/notifications/dispatcher.py`, `src/notifications/repository.py`
- Create: `src/notifications/channels/__init__.py`, `src/notifications/channels/in_app.py`, `src/notifications/channels/macos.py`, `src/notifications/channels/external.py`
- Test: `tests/test_notifications.py`

Full implementation code for this task is in the design spec. The key contract is:

- `NotificationDispatcher.notify(type, title, body, reference_id, channels, priority, dedupe_window_minutes)`
- `NotificationRepository`: create, find_recent, list_notifications, count_unread, dismiss
- Channels: in_app (WebSocket), macos (osascript), external (webhook + email)

- [ ] **Step 1: Write test** (see spec for full test code)
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement repository** (`src/notifications/repository.py`)
- [ ] **Step 4: Implement channels** (`src/notifications/channels/`)
- [ ] **Step 5: Implement dispatcher** (`src/notifications/dispatcher.py`)
- [ ] **Step 6: Run test to verify it passes**
- [ ] **Step 7: Commit**

---

### Task 4: Scheduler

**Files:**

- Create: `src/scheduler.py`
- Test: `tests/test_scheduler.py`

Lightweight asyncio scheduler with `register(name, func, interval_seconds)` and `start()`/`stop()` lifecycle. Ticks every 1s, runs due jobs, logs errors without crashing.

- [ ] **Step 1: Write test** (see spec for full test code)
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement scheduler**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit**

---

## Phase 2: Recurring Meeting Detection

### Task 5: Series Repository

**Files:**

- Create: `src/series/__init__.py`, `src/series/repository.py`
- Test: `tests/test_series_repository.py`

CRUD for `meeting_series` table: create, get, list_all, update, delete, link_meeting, unlink_meeting, get_meetings, find_by_calendar_id.

- [ ] **Step 1-5: TDD cycle** (see spec for full code)
- [ ] **Step 6: Commit**

---

### Task 6: Heuristic Series Detector

**Files:**

- Create: `src/series/detector.py`
- Test: `tests/test_series_detector.py`

Clusters unlinked meetings by attendee overlap (>=60%), day-of-week (+-1), time slot (+-1h), title similarity (>=0.7). Creates series when cluster meets minimum size.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

### Task 7: Series API Routes

**Files:**

- Create: `src/api/routes/series.py`
- Test: `tests/test_api_series.py`

Routes: GET/POST/PATCH/DELETE `/api/series`, POST `/api/series/{id}/meetings`, GET `/api/series/{id}/trends`.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

## Phase 3: Action Item Extraction & Tracking

### Task 8: Action Items Repository

**Files:**

- Create: `src/action_items/__init__.py`, `src/action_items/repository.py`
- Test: `tests/test_action_items_repository.py`

CRUD with lifecycle: create, get, update (auto-sets completed_at), delete, list_items (filterable), list_by_meeting, list_overdue, list_due_reminders.

- [ ] **Step 1-5: TDD cycle** (see spec for full code)
- [ ] **Step 6: Commit**

---

### Task 9: Action Item Extractor (LLM)

**Files:**

- Create: `src/action_items/extractor.py`
- Test: `tests/test_action_items_extractor.py`

Uses same LLM backend as summariser. Extracts JSON array of {title, assignee, due_date, priority, extracted_text}. Handles markdown fences, malformed JSON, retries.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

### Task 10: Action Items API Routes

**Files:**

- Create: `src/api/routes/action_items.py`
- Test: `tests/test_api_action_items.py`

Routes: GET/POST `/api/action-items`, GET/PATCH/DELETE `/api/action-items/{id}`, GET `/api/meetings/{id}/action-items`.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

## Phase 4: Analytics Engine

### Task 11: Analytics Repository & Engine

**Files:**

- Create: `src/analytics/__init__.py`, `src/analytics/repository.py`, `src/analytics/engine.py`
- Test: `tests/test_analytics.py`

Repository: upsert, get_period, get_range. Engine: refresh_period, refresh_current_periods, compute_load_score, get_health_indicators, get_most_met_people.

- [ ] **Step 1-5: TDD cycle** (see spec for full code)
- [ ] **Step 6: Commit**

---

### Task 12: Analytics API Routes

**Files:**

- Create: `src/api/routes/analytics.py`
- Test: `tests/test_api_analytics.py`

Routes: GET `/api/analytics/summary`, `/api/analytics/trends`, `/api/analytics/people`, `/api/analytics/health`, POST `/api/analytics/refresh`.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

## Phase 5: Meeting Preparation Mode

### Task 13: Prep Briefing Generator

**Files:**

- Create: `src/prep/__init__.py`, `src/prep/repository.py`, `src/prep/briefing.py`
- Test: `tests/test_prep.py`

Repository: create, get, get_upcoming, get_by_meeting. Generator: gather_context (series history, attendee meetings, open action items), generate (LLM call), fallback without LLM.

- [ ] **Step 1-5: TDD cycle** (see spec for full code)
- [ ] **Step 6: Commit**

---

## Phase 6: Pipeline & Scheduler Integration

### Task 14: Wire Into Pipeline and Server

**Files:**

- Modify: `src/main.py` (add `_run_post_processing`, `_post_process_async`, `_extract_action_items`, `_refresh_analytics`)
- Modify: `src/api/server.py` (add scheduler setup, register jobs, wire new routes)
- Test: `tests/test_pipeline_integration.py`

Key changes:

- After `pipeline.complete` event, call `_run_post_processing(meeting_id, transcript)`
- In `ApiServer._run_async()`, create and start `Scheduler` with registered jobs
- In `_create_app()`, init and include new route modules (action_items, series, analytics, notifications, prep)

- [ ] **Step 1-4: Implementation and test** (see spec for full code)
- [ ] **Step 5: Commit**

---

### Task 15: Notifications API Routes

**Files:**

- Create: `src/api/routes/notifications.py`
- Test: `tests/test_api_notifications.py`

Routes: GET `/api/notifications`, GET `/api/notifications/unread-count`, PATCH `/api/notifications/{id}`.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

### Task 16: Prep API Routes

**Files:**

- Create: `src/api/routes/prep.py`
- Test: `tests/test_api_prep.py`

Routes: GET `/api/prep/upcoming`, GET `/api/prep/{meeting_id}`, POST `/api/prep/{meeting_id}/generate`.

- [ ] **Step 1-4: TDD cycle** (see spec for full code)
- [ ] **Step 5: Commit**

---

### Task 17: Final Integration Test & Lint

- [ ] **Step 1: Run full test suite** (`python3 -m pytest tests/ -v`)
- [ ] **Step 2: Run linter** (`ruff check src/ tests/`)
- [ ] **Step 3: Verify all imports** (`python3 -c "from src.main import MeetingMind; ..."`)
- [ ] **Step 4: Commit any fixes**

---

## Summary

| Phase           | Tasks     | What Ships                                          |
| --------------- | --------- | --------------------------------------------------- |
| 1: Foundation   | 1-4       | Schema v9, config, notifications, scheduler         |
| 2: Series       | 5-7       | Series repo, heuristic detector, API                |
| 3: Action Items | 8-10      | Repository, LLM extractor, API                      |
| 4: Analytics    | 11-12     | Engine, pre-computation, API                        |
| 5: Prep Mode    | 13, 16    | Briefing generator, repo, API                       |
| 6: Integration  | 14-15, 17 | Pipeline hooks, scheduler wiring, notifications API |

Each task is independently testable. The pipeline never breaks as new post-processing steps fail gracefully. UI implementation follows as a separate plan once the backend is validated.
