"""Tests for schema v12 migration (people directory + voice profiles).

v12 adds the ``people`` and ``voice_profiles`` tables and links
``speaker_mappings`` rows to people via ``person_id`` (+ ``confidence``
for automatic voice matches).
"""

import time

import aiosqlite
import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_schema_version_covers_v12():
    assert SCHEMA_VERSION >= 12


@pytest.mark.asyncio
async def test_fresh_install_has_people_and_voice_tables(tmp_path):
    db = Database(db_path=tmp_path / "fresh_v12.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('people', 'voice_profiles') ORDER BY name"
        )
        names = [r[0] for r in await cursor.fetchall()]
        assert names == ["people", "voice_profiles"]

        cursor = await db.conn.execute("PRAGMA table_info(speaker_mappings)")
        cols = {r[1] for r in await cursor.fetchall()}
        assert "person_id" in cols
        assert "confidence" in cols
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_from_v11_adds_tables_and_columns(tmp_path):
    db_path = tmp_path / "v11_migrate.db"

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
                updated_at REAL NOT NULL,
                notion_page_id TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS speaker_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                speaker_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                created_at REAL NOT NULL,
                UNIQUE(meeting_id, speaker_id)
            );
        """)
        now = time.time()
        await conn.execute(
            """INSERT INTO speaker_mappings
               (meeting_id, speaker_id, display_name, source, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("m1", "Remote", "Sarah", "manual", now),
        )
        await conn.execute("PRAGMA user_version = 11")
        await conn.commit()

    db = Database(db_path=db_path)
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        # Existing mapping preserved, new columns present and NULL.
        cursor = await db.conn.execute(
            "SELECT display_name, person_id, confidence FROM speaker_mappings "
            "WHERE meeting_id = 'm1'"
        )
        row = await cursor.fetchone()
        assert row["display_name"] == "Sarah"
        assert row["person_id"] is None
        assert row["confidence"] is None

        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='people'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_speaker_mapping_person_link_round_trips(tmp_path):
    from src.db.repository import MeetingRepository
    from src.people.repository import PersonRepository

    db = Database(db_path=tmp_path / "roundtrip.db")
    await db.connect()
    try:
        repo = MeetingRepository(db)
        person_repo = PersonRepository(db)
        meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
        person_id = await person_repo.create(name="Sarah")

        await repo.set_speaker_name(
            meeting_id, "Remote", "Sarah", source="voice", person_id=person_id, confidence=0.83
        )

        mappings = await repo.get_speaker_names(meeting_id)
        assert len(mappings) == 1
        assert mappings[0]["person_id"] == person_id
        assert mappings[0]["confidence"] == pytest.approx(0.83)
        assert mappings[0]["source"] == "voice"
    finally:
        await db.close()
