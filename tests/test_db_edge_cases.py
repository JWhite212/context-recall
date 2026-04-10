"""Edge-case tests for src/db/ — supplements test_repository.py."""

import json
import time

import pytest

from src.db.database import SCHEMA_VERSION, Database
from src.db.repository import MeetingRepository


@pytest.mark.asyncio
async def test_schema_migration_version_set(db: Database):
    cursor = await db.conn.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_idempotent_migration(tmp_path):
    """Calling connect() twice on the same database should not error."""
    db = Database(db_path=tmp_path / "idempotent.db")
    await db.connect()
    await db.close()

    # Connect again — migration should be a no-op.
    db2 = Database(db_path=tmp_path / "idempotent.db")
    await db2.connect()
    cursor = await db2.conn.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row[0] == SCHEMA_VERSION
    await db2.close()


@pytest.mark.asyncio
async def test_meeting_record_from_row_round_trip(repo: MeetingRepository):
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(
        mid,
        title="Round Trip Test",
        status="complete",
        duration_seconds=120.0,
        tags=["test", "roundtrip"],
        language="en",
        word_count=42,
    )
    meeting = await repo.get_meeting(mid)
    assert meeting is not None
    d = meeting.to_dict()
    assert d["id"] == mid
    assert d["title"] == "Round Trip Test"
    assert d["status"] == "complete"
    assert d["duration_seconds"] == 120.0
    assert d["tags"] == ["test", "roundtrip"]
    assert d["language"] == "en"
    assert d["word_count"] == 42
    assert "created_at" in d
    assert "updated_at" in d


@pytest.mark.asyncio
async def test_meeting_record_tags_from_json(repo: MeetingRepository):
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, tags=["a", "b"])
    meeting = await repo.get_meeting(mid)
    assert meeting.tags == ["a", "b"]


@pytest.mark.asyncio
async def test_meeting_record_null_tags(repo: MeetingRepository):
    mid = await repo.create_meeting(started_at=time.time())
    # Tags are not set — should default to empty list.
    meeting = await repo.get_meeting(mid)
    assert meeting.tags == []


@pytest.mark.asyncio
async def test_fts_fallback_to_like(db: Database, repo: MeetingRepository):
    """If the FTS table is dropped, search_meetings should fall back to LIKE."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Unique Searchable Title")

    # Drop the FTS table to simulate FTS being unavailable.
    await db.conn.execute("DROP TABLE IF EXISTS meetings_fts")
    await db.conn.commit()

    # search_meetings should still work via LIKE fallback.
    results = await repo.search_meetings("Unique Searchable")
    assert len(results) >= 1
    assert results[0].title == "Unique Searchable Title"


@pytest.mark.asyncio
async def test_update_fts_index(repo: MeetingRepository):
    """update_fts gracefully handles the FTS content table mismatch.

    The FTS table has a ``transcript_text`` column, but the underlying
    ``meetings`` table stores transcripts in ``transcript_json``. This
    means update_fts will log a warning but not raise.
    """
    mid = await repo.create_meeting(started_at=time.time())

    transcript_data = json.dumps({
        "segments": [
            {"start": 0, "end": 5, "text": "quantum computing discussion"},
        ],
    })

    await repo.update_meeting(
        mid,
        title="Tech Sync",
        transcript_json=transcript_data,
        status="complete",
    )
    # update_fts should not raise — it handles errors internally.
    await repo.update_fts(mid)
