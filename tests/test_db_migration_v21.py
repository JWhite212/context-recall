"""v21 migration: add meetings.title_source + meetings.markdown_path."""

import pytest

from src.db.database import SCHEMA_VERSION, Database
from src.db.repository import MeetingRepository


@pytest.mark.asyncio
async def test_v21_adds_title_source_and_markdown_path(tmp_path):
    db = Database(db_path=tmp_path / "m.db")
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "title_source" in cols
        assert "markdown_path" in cols

        # A fresh DB fast-forwards to the current head version via the
        # fresh-create path, not the versioned `< 21` migration block, so
        # this must track SCHEMA_VERSION rather than a version literal.
        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == SCHEMA_VERSION

        repo = MeetingRepository(db)
        mid = await repo.create_meeting(started_at=1.0, status="complete")
        m = await repo.get_meeting(mid)
        assert m.title_source == "auto"
        assert m.markdown_path == ""
    finally:
        await db.close()
