"""Tests for structured fields + scoped per-definition result replace on InsightRepository."""

from src.db.database import Database
from src.insights.repository import InsightRepository


async def _repo(tmp_path):
    db = Database(db_path=tmp_path / "ins.db")
    await db.connect()
    return db, InsightRepository(db)


async def test_create_structured_definition_roundtrips_fields(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        fields = [
            {"key": "go_live_date", "label": "Go-live date", "type": "date"},
            {"key": "blockers", "label": "Blockers", "type": "list"},
        ]
        did = await repo.create(
            "Client Call", "Extract client details", output_mode="structured", fields=fields
        )
        got = await repo.get(did)
        assert got["output_mode"] == "structured"
        assert got["fields"] == fields
    finally:
        await db.close()


async def test_list_definition_defaults_to_list_mode(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        did = await repo.create("Questions", "List questions asked")
        got = await repo.get(did)
        assert got["output_mode"] == "list"
        assert got["fields"] is None
    finally:
        await db.close()


async def test_replace_results_for_definition_isolates_definitions(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        from src.db.repository import MeetingRepository

        mrepo = MeetingRepository(db)
        mid = await mrepo.create_meeting(started_at=1.0)
        # Global write of two definitions' results.
        await repo.replace_results_for_meeting(
            mid,
            [
                {"definition_id": "A", "definition_name": "A", "content": "a1", "speaker": ""},
                {"definition_id": "B", "definition_name": "B", "content": "b1", "speaker": ""},
            ],
        )
        # Scoped re-run of B only must not touch A.
        n = await repo.replace_results_for_definition(
            mid,
            "B",
            [
                {
                    "definition_id": "B",
                    "definition_name": "B",
                    "content": "b2",
                    "speaker": "",
                    "fields": {"x": 1},
                },
            ],
        )
        assert n == 1
        rows = await repo.results_for_meeting(mid)
        contents = {(r["definition_id"], r["content"]) for r in rows}
        assert ("A", "a1") in contents
        assert ("B", "b2") in contents
        assert ("B", "b1") not in contents
        b_row = next(r for r in rows if r["definition_id"] == "B")
        assert b_row["fields"] == {"x": 1}
    finally:
        await db.close()
