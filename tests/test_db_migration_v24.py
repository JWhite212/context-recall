"""v24 migration: meetings.calendar_event_uid forward link to a calendar entry."""

from src.db.database import SCHEMA_VERSION, Database


async def test_v23_db_migrates_to_v24_with_calendar_event_uid(tmp_path):
    db_path = tmp_path / "v23.db"
    db = Database(db_path=db_path)
    await db.connect()
    # Seed a meeting, then rewind to 23 so the v24 block runs on reconnect.
    await db.conn.execute(
        "INSERT INTO meetings (id, title, started_at, status, created_at, updated_at) "
        "VALUES ('m1', 'Chat', 1000.0, 'complete', 1.0, 1.0)"
    )
    await db.conn.execute("PRAGMA user_version = 23")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 24
        # Column exists and defaults to ''.
        cur = await db2.conn.execute("SELECT calendar_event_uid FROM meetings WHERE id = 'm1'")
        assert (await cur.fetchone())["calendar_event_uid"] == ""
    finally:
        await db2.close()


async def test_v24_migration_survives_missing_meetings_table(tmp_path):
    """A partial/legacy DB rewound below 24 without a meetings table must not
    hard-fail the ALTER (mirrors v21/v22/v23 defensive guards)."""
    db_path = tmp_path / "legacy.db"
    db = Database(db_path=db_path)
    await db.connect()
    await db.conn.execute("DROP TABLE meetings")
    await db.conn.execute("PRAGMA user_version = 23")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()  # must not raise
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 24
    finally:
        await db2.close()
