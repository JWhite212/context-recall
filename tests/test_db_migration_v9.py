"""Tests for schema v9 migration (meeting intelligence tables)."""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_migration_creates_new_tables(tmp_path):
    """Fresh DB at v9 should have all new tables and meetings.series_id column."""
    db = Database(db_path=tmp_path / "fresh_v9.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION == 9

        # Check that user_version is set to 9.
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == 9

        # Verify all new tables exist by querying sqlite_master.
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = {r[0] for r in rows}

        assert "meeting_series" in table_names
        assert "action_items" in table_names
        assert "meeting_analytics" in table_names
        assert "notifications" in table_names
        assert "prep_briefings" in table_names

        # Verify meetings.series_id column exists.
        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        columns = {r[1] for r in await cursor.fetchall()}
        assert "series_id" in columns

        # Verify meeting_series has expected columns.
        cursor = await db.conn.execute("PRAGMA table_info(meeting_series)")
        ms_cols = {r[1] for r in await cursor.fetchall()}
        for col in (
            "id",
            "title",
            "calendar_series_id",
            "detection_method",
            "typical_attendees_json",
            "typical_day_of_week",
            "typical_time",
            "typical_duration_minutes",
            "created_at",
            "updated_at",
        ):
            assert col in ms_cols, f"meeting_series missing column: {col}"

        # Verify action_items has expected columns.
        cursor = await db.conn.execute("PRAGMA table_info(action_items)")
        ai_cols = {r[1] for r in await cursor.fetchall()}
        for col in (
            "id",
            "meeting_id",
            "title",
            "description",
            "assignee",
            "status",
            "priority",
            "due_date",
            "reminder_at",
            "source",
            "extracted_text",
            "created_at",
            "updated_at",
            "completed_at",
        ):
            assert col in ai_cols, f"action_items missing column: {col}"

        # Verify meeting_analytics has expected columns.
        cursor = await db.conn.execute("PRAGMA table_info(meeting_analytics)")
        ma_cols = {r[1] for r in await cursor.fetchall()}
        for col in (
            "id",
            "period_type",
            "period_start",
            "total_meetings",
            "total_duration_minutes",
            "total_words",
            "unique_attendees",
            "recurring_ratio",
            "action_items_created",
            "action_items_completed",
            "busiest_hour",
            "computed_at",
        ):
            assert col in ma_cols, f"meeting_analytics missing column: {col}"

        # Verify notifications has expected columns.
        cursor = await db.conn.execute("PRAGMA table_info(notifications)")
        n_cols = {r[1] for r in await cursor.fetchall()}
        for col in (
            "id",
            "type",
            "reference_id",
            "channel",
            "title",
            "body",
            "status",
            "scheduled_at",
            "sent_at",
            "created_at",
        ):
            assert col in n_cols, f"notifications missing column: {col}"

        # Verify prep_briefings has expected columns.
        cursor = await db.conn.execute("PRAGMA table_info(prep_briefings)")
        pb_cols = {r[1] for r in await cursor.fetchall()}
        for col in (
            "id",
            "meeting_id",
            "series_id",
            "content_markdown",
            "attendees_json",
            "related_meeting_ids_json",
            "open_action_items_json",
            "generated_at",
            "expires_at",
        ):
            assert col in pb_cols, f"prep_briefings missing column: {col}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v8(tmp_path):
    """Existing v8 DB should migrate to v9 without data loss."""
    db_path = tmp_path / "v8_migrate.db"

    # Manually create a v8 database with a meeting row.
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
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
            );
            CREATE INDEX IF NOT EXISTS idx_meetings_started_at ON meetings(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);

            CREATE TABLE IF NOT EXISTS speaker_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                speaker_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                created_at REAL NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
                UNIQUE(meeting_id, speaker_id)
            );

            CREATE TABLE IF NOT EXISTS segment_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                text TEXT NOT NULL,
                speaker TEXT DEFAULT '',
                start_time REAL NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            );
        """)

        # Insert a test meeting.
        now = time.time()
        await conn.execute(
            """INSERT INTO meetings
               (id, title, started_at, status, created_at, updated_at, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test-meeting-1", "V8 Meeting", now, "complete", now, now, '["v8"]'),
        )
        await conn.execute("PRAGMA user_version = 8")
        await conn.commit()

    # Now open via Database which should trigger migration to v9.
    db = Database(db_path=db_path)
    await db.connect()
    try:
        # Verify version bumped to 9.
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == 9

        # Verify old data is preserved.
        cursor = await db.conn.execute("SELECT * FROM meetings WHERE id = ?", ("test-meeting-1",))
        row = await cursor.fetchone()
        assert row is not None
        assert row["title"] == "V8 Meeting"
        assert row["status"] == "complete"

        # series_id should default to NULL on existing rows.
        assert row["series_id"] is None

        # Verify new tables exist.
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = {r[0] for r in rows}
        assert "meeting_series" in table_names
        assert "action_items" in table_names
        assert "meeting_analytics" in table_names
        assert "notifications" in table_names
        assert "prep_briefings" in table_names
    finally:
        await db.close()
