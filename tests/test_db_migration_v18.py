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
            "event_uid",
            "title",
            "start_ts",
            "end_ts",
            "attendees_json",
            "organizer_json",
            "join_url",
            "meeting_id",
            "calendar_name",
            "recorded_meeting_id",
            "synced_at",
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
