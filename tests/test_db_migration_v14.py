"""Tests for schema v14 migration (keyword trackers)."""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_version_covers_v14():
    assert SCHEMA_VERSION >= 14


@pytest.mark.asyncio
async def test_fresh_install_has_tracker_tables(tmp_path):
    db = Database(db_path=tmp_path / "fresh_v14.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('trackers', 'tracker_hits') ORDER BY name"
        )
        assert [r[0] for r in await cursor.fetchall()] == ["tracker_hits", "trackers"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v13_adds_tracker_tables(tmp_path):
    db_path = tmp_path / "v13_migrate.db"

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Untitled',
                started_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'recording',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        now = time.time()
        await conn.execute(
            "INSERT INTO meetings (id, title, started_at, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("m-old", "Old", now, "complete", now, now),
        )
        await conn.execute("PRAGMA user_version = 13")
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trackers'"
        )
        assert await cursor.fetchone() is not None
        cursor = await db.conn.execute("SELECT title FROM meetings WHERE id='m-old'")
        assert (await cursor.fetchone())["title"] == "Old"
    finally:
        await db.close()
