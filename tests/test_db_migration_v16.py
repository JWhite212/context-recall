"""Schema v16 migration: custom insight_definitions + insight_results tables."""

import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v16_creates_insight_tables(tmp_path):
    db = Database(db_path=tmp_path / "v16.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 16
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('insight_definitions','insight_results') ORDER BY name"
        )
        assert [r[0] for r in await cur.fetchall()] == [
            "insight_definitions",
            "insight_results",
        ]
    finally:
        await db.close()


async def test_v16_upgrade_from_v15_preserves_data(tmp_path):
    db_path = tmp_path / "v15old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, started_at REAL)")
        await conn.execute("INSERT INTO meetings (id, started_at) VALUES ('m1', 1.0)")
        await conn.execute("PRAGMA user_version = 15")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='insight_definitions'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("SELECT id FROM meetings WHERE id='m1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
