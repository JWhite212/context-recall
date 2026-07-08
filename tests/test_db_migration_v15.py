"""Schema v15 migration: per-meeting template_name / template_source columns."""

from src.db.database import SCHEMA_VERSION, Database
from src.db.repository import MeetingRepository


async def test_v15_adds_template_columns(tmp_path):
    db = Database(db_path=tmp_path / "v15.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 15
        cur = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {r[1] for r in await cur.fetchall()}
        assert {"template_name", "template_source"} <= cols
    finally:
        await db.close()


async def test_v15_round_trips_template_fields(tmp_path):
    db = Database(db_path=tmp_path / "v15b.db")
    await db.connect()
    repo = MeetingRepository(db)
    try:
        mid = await repo.create_meeting(started_at=1000.0)
        await repo.update_meeting(mid, template_name="discovery", template_source="manual")
        m = await repo.get_meeting(mid)
        assert m.template_name == "discovery"
        assert m.template_source == "manual"
    finally:
        await db.close()
