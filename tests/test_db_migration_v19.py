import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v19_adds_prep_event_columns(tmp_path):
    db = Database(db_path=tmp_path / "v19.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 19
        cur = await db.conn.execute("PRAGMA table_info(prep_briefings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "calendar_event_uid" in cols
        assert "event_signature" in cols
    finally:
        await db.close()


async def test_v19_upgrade_from_v18_preserves_data(tmp_path):
    db_path = tmp_path / "v18old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE prep_briefings (id TEXT PRIMARY KEY, meeting_id TEXT, "
            "content_markdown TEXT, generated_at REAL, expires_at REAL)"
        )
        await conn.execute(
            "INSERT INTO prep_briefings (id, content_markdown, generated_at, expires_at) "
            "VALUES ('p1', 'hi', 1.0, 9999999999.0)"
        )
        await conn.execute("PRAGMA user_version = 18")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute("PRAGMA table_info(prep_briefings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"calendar_event_uid", "event_signature"} <= cols
        cur = await db.conn.execute("SELECT id FROM prep_briefings WHERE id='p1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
