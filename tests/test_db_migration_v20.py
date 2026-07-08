"""Migration v19 -> v20: fold the single `label` into the `tags` array.

v20 adds no tables or columns (pure data move), so a head database rewound
to user_version 19 is a faithful pre-fold state to migrate from.
"""

import json

from src.db.database import SCHEMA_VERSION, Database
from src.db.repository import MeetingRepository


async def test_migration_folds_label_into_tags(tmp_path):
    db_path = tmp_path / "v19_fold.db"

    db = Database(db_path=db_path)
    await db.connect()
    repo = MeetingRepository(db)
    meeting_id = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(meeting_id, tags=["standup"], label="ClientX")
    # Rewind so the < 20 migration re-runs on the next connect().
    await db.conn.execute("PRAGMA user_version = 19")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db2.conn.execute("SELECT tags FROM meetings WHERE id = ?", (meeting_id,))
        tags = json.loads((await cur.fetchone())["tags"])
        assert "standup" in tags
        assert "ClientX" in tags
    finally:
        await db2.close()


async def test_migration_leaves_empty_label_meetings_untouched(tmp_path):
    db_path = tmp_path / "v19_empty.db"

    db = Database(db_path=db_path)
    await db.connect()
    repo = MeetingRepository(db)
    meeting_id = await repo.create_meeting(started_at=1000.0)
    await repo.update_meeting(meeting_id, tags=["standup"])
    await db.conn.execute("PRAGMA user_version = 19")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("SELECT tags FROM meetings WHERE id = ?", (meeting_id,))
        assert json.loads((await cur.fetchone())["tags"]) == ["standup"]
    finally:
        await db2.close()
