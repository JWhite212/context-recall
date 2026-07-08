"""Tests for schema v13 migration (clients + projects + meeting assignment)."""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_version_covers_v13():
    assert SCHEMA_VERSION >= 13


@pytest.mark.asyncio
async def test_fresh_install_has_clients_projects_and_assignment_columns(tmp_path):
    db = Database(db_path=tmp_path / "fresh_v13.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('clients', 'projects') ORDER BY name"
        )
        assert [r[0] for r in await cursor.fetchall()] == ["clients", "projects"]

        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert {"client_id", "project_id", "assignment_source", "assignment_confidence"} <= cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v12_adds_tables_and_preserves_data(tmp_path):
    db_path = tmp_path / "v12_migrate.db"

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
            ("m-old", "Old Meeting", now, "complete", now, now),
        )
        await conn.execute("PRAGMA user_version = 12")
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION

        cursor = await db.conn.execute(
            "SELECT title, client_id, assignment_source FROM meetings WHERE id = 'm-old'"
        )
        row = await cursor.fetchone()
        assert row["title"] == "Old Meeting"
        assert row["client_id"] is None
        assert (row["assignment_source"] or "") == ""
    finally:
        await db.close()
