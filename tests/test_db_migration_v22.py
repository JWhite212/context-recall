"""v22 migration: add client_id / project_id / tag_source to action_items."""

import pytest

from src.db.database import SCHEMA_VERSION, Database


@pytest.mark.asyncio
async def test_v22_adds_action_item_tag_columns(tmp_path):
    db = Database(db_path=tmp_path / "m.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA table_info(action_items)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "client_id" in cols
        assert "project_id" in cols
        assert "tag_source" in cols

        # A fresh DB fast-forwards to the current head version via the
        # fresh-create path, not the versioned `< 22` migration block, so
        # this must track SCHEMA_VERSION rather than a version literal.
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION
    finally:
        await db.close()
