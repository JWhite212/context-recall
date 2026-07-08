"""Tests for schema v11 migration (Notion page identity).

v11 adds ``meetings.notion_page_id`` so a reprocess can archive the
previously written Notion page and store the replacement's id instead
of accumulating duplicate pages on every re-run.
"""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_version_covers_v11():
    assert SCHEMA_VERSION >= 11


@pytest.mark.asyncio
async def test_fresh_install_has_notion_page_id_column(tmp_path):
    db = Database(db_path=tmp_path / "fresh_v11.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert "notion_page_id" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v10_adds_column_and_preserves_data(tmp_path):
    db_path = tmp_path / "v10_migrate.db"

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
            CREATE TABLE IF NOT EXISTS reprocess_jobs (
                meeting_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_flight'
            );
        """)
        now = time.time()
        await conn.execute(
            """INSERT INTO meetings
               (id, title, started_at, status, created_at, updated_at, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("v10-meeting", "V10 Meeting", now, "complete", now, now, "[]"),
        )
        await conn.execute("PRAGMA user_version = 10")
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert "notion_page_id" in cols

        cursor = await db.conn.execute(
            "SELECT title, notion_page_id FROM meetings WHERE id = ?", ("v10-meeting",)
        )
        row = await cursor.fetchone()
        assert row["title"] == "V10 Meeting"
        assert (row["notion_page_id"] or "") == ""
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_notion_page_id_round_trips_through_repository(tmp_path):
    from src.db.repository import MeetingRepository

    db = Database(db_path=tmp_path / "roundtrip.db")
    await db.connect()
    try:
        repo = MeetingRepository(db)
        meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
        await repo.update_meeting(meeting_id, notion_page_id="page-abc")

        meeting = await repo.get_meeting(meeting_id)
        assert meeting.notion_page_id == "page-abc"
        assert meeting.to_dict()["notion_page_id"] == "page-abc"
    finally:
        await db.close()
