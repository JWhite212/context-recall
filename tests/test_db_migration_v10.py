"""Tests for schema v10 migration (reprocess_jobs durability).

v10 introduces a ``reprocess_jobs`` table so the reprocess endpoint can
track in-flight pipelines durably across daemon restarts. Without this,
a daemon crash mid-reprocess left the meeting row in ``transcribing``
forever with no UI affordance to retry.
"""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_version_is_v10():
    assert SCHEMA_VERSION == 10


@pytest.mark.asyncio
async def test_fresh_install_creates_reprocess_jobs_table(tmp_path):
    """A fresh DB at v10 should have the reprocess_jobs table with the
    documented columns and primary key."""
    db = Database(db_path=tmp_path / "fresh_v10.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == 10

        # Table exists.
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reprocess_jobs'"
        )
        assert await cursor.fetchone() is not None, (
            "reprocess_jobs table must exist after fresh v10 install"
        )

        # Expected columns with expected types.
        cursor = await db.conn.execute("PRAGMA table_info(reprocess_jobs)")
        cols = {r[1]: r for r in await cursor.fetchall()}
        assert "meeting_id" in cols
        assert "started_at" in cols
        assert "status" in cols

        # meeting_id is the primary key (column 5 == pk flag).
        assert cols["meeting_id"][5] == 1, "meeting_id must be the primary key"

        # status defaults to 'in_flight'.
        await db.conn.execute(
            "INSERT INTO reprocess_jobs (meeting_id, started_at) VALUES (?, ?)",
            ("m1", time.time()),
        )
        cursor = await db.conn.execute(
            "SELECT status FROM reprocess_jobs WHERE meeting_id = ?", ("m1",)
        )
        row = await cursor.fetchone()
        assert row[0] == "in_flight"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v9_creates_reprocess_jobs(tmp_path):
    """An existing v9 DB must migrate to v10 without losing data and gain
    the ``reprocess_jobs`` table."""
    db_path = tmp_path / "v9_migrate.db"

    # Build a minimal v9 database that the migration will recognize.
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
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
                teams_meeting_id TEXT DEFAULT '',
                series_id TEXT DEFAULT NULL
            );
        """)
        now = time.time()
        await conn.execute(
            """INSERT INTO meetings
               (id, title, started_at, status, created_at, updated_at, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("v9-meeting", "V9 Meeting", now, "complete", now, now, "[]"),
        )
        await conn.execute("PRAGMA user_version = 9")
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == 10

        # Old data preserved.
        cursor = await db.conn.execute("SELECT title FROM meetings WHERE id = ?", ("v9-meeting",))
        row = await cursor.fetchone()
        assert row is not None
        assert row["title"] == "V9 Meeting"

        # New table exists.
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reprocess_jobs'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Running the migration twice against an already-v10 DB must be a no-op."""
    db_path = tmp_path / "twice.db"

    db = Database(db_path=db_path)
    await db.connect()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cursor = await db2.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == 10

        # Table still exists and is still empty.
        cursor = await db2.conn.execute("SELECT COUNT(*) FROM reprocess_jobs")
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await db2.close()
