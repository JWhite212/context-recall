"""v23 migration: structured-insight columns + app_metadata KV store."""

from src.db.database import SCHEMA_VERSION, Database


async def test_v22_db_migrates_to_v23_with_new_columns(tmp_path):
    db_path = tmp_path / "v22.db"
    db = Database(db_path=db_path)
    await db.connect()
    # Insert a pre-v23 insight definition, then rewind to 22.
    await db.conn.execute(
        "INSERT INTO insight_definitions (id, name, prompt, enabled, created_at, updated_at) "
        "VALUES ('d1', 'Questions', 'List questions', 1, 1.0, 1.0)"
    )
    await db.conn.execute("PRAGMA user_version = 22")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 23
        # Old row defaults to list mode, null fields.
        cur = await db2.conn.execute(
            "SELECT output_mode, fields_json FROM insight_definitions WHERE id = 'd1'"
        )
        row = await cur.fetchone()
        assert row["output_mode"] == "list"
        assert row["fields_json"] is None
        # app_metadata usable.
        await db2.conn.execute("INSERT INTO app_metadata (key, value) VALUES ('k', 'v')")
        await db2.conn.commit()
        cur = await db2.conn.execute("SELECT value FROM app_metadata WHERE key = 'k'")
        assert (await cur.fetchone())["value"] == "v"
    finally:
        await db2.close()


async def test_get_set_meta_roundtrip(tmp_path):
    from src.db.repository import MeetingRepository

    db = Database(db_path=tmp_path / "meta.db")
    await db.connect()
    try:
        repo = MeetingRepository(db)
        assert await repo.get_meta("missing") is None
        await repo.set_meta("insights_seed_version", "1")
        assert await repo.get_meta("insights_seed_version") == "1"
        await repo.set_meta("insights_seed_version", "2")  # upsert
        assert await repo.get_meta("insights_seed_version") == "2"
    finally:
        await db.close()
