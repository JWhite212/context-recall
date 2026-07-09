import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v17_creates_automation_tables(tmp_path):
    db = Database(db_path=tmp_path / "v17.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 17
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('automation_rules','automation_dispatches') ORDER BY name"
        )
        assert [r[0] for r in await cur.fetchall()] == [
            "automation_dispatches",
            "automation_rules",
        ]
    finally:
        await db.close()


async def test_v17_upgrade_from_v16_preserves_data(tmp_path):
    db_path = tmp_path / "v16old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, started_at REAL)")
        await conn.execute("INSERT INTO meetings (id, started_at) VALUES ('m1', 1.0)")
        await conn.execute("PRAGMA user_version = 16")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='automation_rules'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("SELECT id FROM meetings WHERE id='m1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
