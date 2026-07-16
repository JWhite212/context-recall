"""Tests for one-time tailored starter-content seeding (insights + automations)."""

from src.automations.repository import AutomationRepository
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.insights.repository import InsightRepository
from src.insights.seed import SEED_VERSION, seed_starter_content


async def _repos(tmp_path):
    db = Database(db_path=tmp_path / "seed.db")
    await db.connect()
    return db, MeetingRepository(db), InsightRepository(db), AutomationRepository(db)


async def test_seeds_structured_insights_and_rules(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        seeded = await seed_starter_content(mrepo, irepo, arepo)
        assert seeded is True
        defs = await irepo.list_definitions()
        names = {d["name"] for d in defs}
        assert {"Client Call Details", "Standup Snapshot", "Discovery Notes"} <= names
        client_call = next(d for d in defs if d["name"] == "Client Call Details")
        assert client_call["output_mode"] == "structured"
        assert any(f["key"] == "go_live_date" for f in client_call["fields"])
        rules = await arepo.list_rules()
        assert len(rules) >= 3
        # A rule references a real seeded definition via run_insight.
        run_ids = [
            a["definition_id"] for r in rules for a in r["actions"] if a["type"] == "run_insight"
        ]
        assert client_call["id"] in run_ids
        assert await mrepo.get_meta("insights_seed_version") == str(SEED_VERSION)
    finally:
        await db.close()


async def test_seed_is_idempotent(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        await seed_starter_content(mrepo, irepo, arepo)
        again = await seed_starter_content(mrepo, irepo, arepo)
        assert again is False
        assert len(await irepo.list_definitions()) == 3
    finally:
        await db.close()


async def test_seed_does_not_rerun_after_user_deletes(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        await seed_starter_content(mrepo, irepo, arepo)
        for d in await irepo.list_definitions():
            await irepo.delete(d["id"])
        await seed_starter_content(mrepo, irepo, arepo)
        assert await irepo.list_definitions() == []
    finally:
        await db.close()
