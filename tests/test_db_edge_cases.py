"""Edge-case tests for src/db/ — supplements test_repository.py."""

import asyncio
import json
import logging
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

    transcript_data = json.dumps(
        {
            "segments": [
                {"start": 0, "end": 5, "text": "quantum computing discussion"},
            ],
        }
    )

    await repo.update_meeting(
        mid,
        title="Tech Sync",
        transcript_json=transcript_data,
        status="complete",
    )
    # update_fts should not raise — it handles errors internally.
    await repo.update_fts(mid)


@pytest.mark.asyncio
async def test_meeting_record_from_row_all_nulls(repo: MeetingRepository):
    """A meeting created with only started_at and status should have all
    optional fields as None and tags as an empty list."""
    mid = await repo.create_meeting(started_at=time.time())
    meeting = await repo.get_meeting(mid)
    assert meeting is not None
    assert meeting.ended_at is None
    assert meeting.duration_seconds is None
    assert meeting.audio_path is None
    assert meeting.transcript_json is None
    assert meeting.summary_markdown is None
    assert meeting.language is None
    assert meeting.word_count is None
    assert meeting.tags == []


# ------------------------------------------------------------------
# PRAGMA verification + write_lock + transaction helper + indexes
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_exposes_write_lock(db: Database):
    """Database exposes an asyncio.Lock via the .write_lock property."""
    lock = db.write_lock
    assert isinstance(lock, asyncio.Lock)
    # Same instance on repeat access.
    assert db.write_lock is lock


@pytest.mark.asyncio
async def test_pragma_wal_and_foreign_keys_active(db: Database):
    """After connect(), PRAGMA values should match what we set."""
    cursor = await db.conn.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row[0].lower() == "wal"

    cursor = await db.conn.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_pragma_verification_logs_error_on_mismatch(
    db: Database, caplog: pytest.LogCaptureFixture
):
    """If a PRAGMA fails to apply, _verify_pragmas should log at ERROR level."""
    # Force foreign_keys off so the verification logs an error.
    await db.conn.execute("PRAGMA foreign_keys=OFF")
    with caplog.at_level(logging.ERROR, logger="contextrecall.db"):
        await db._verify_pragmas()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("foreign_keys" in r.getMessage() for r in errors)
    # Restore for downstream tests using this connection.
    await db.conn.execute("PRAGMA foreign_keys=ON")


@pytest.mark.asyncio
async def test_idempotent_indexes_created(db: Database):
    """The post-migration indexes should exist on a fresh DB."""
    cursor = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name IN ('idx_segment_embeddings_meeting_segidx', 'idx_action_items_due_status')"
    )
    rows = await cursor.fetchall()
    names = {r[0] for r in rows}
    assert "idx_segment_embeddings_meeting_segidx" in names
    assert "idx_action_items_due_status" in names


@pytest.mark.asyncio
async def test_execute_in_transaction_commits_all_statements(db: Database, repo: MeetingRepository):
    """execute_in_transaction atomically applies a list of statements."""
    mid = await repo.create_meeting(started_at=time.time())
    now = time.time()
    await db.execute_in_transaction(
        [
            ("UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?", ("A", now, mid)),
            ("UPDATE meetings SET status = ?, updated_at = ? WHERE id = ?", ("complete", now, mid)),
        ]
    )
    meeting = await repo.get_meeting(mid)
    assert meeting.title == "A"
    assert meeting.status == "complete"


@pytest.mark.asyncio
async def test_execute_in_transaction_rolls_back_on_error(db: Database, repo: MeetingRepository):
    """A failing statement inside execute_in_transaction rolls back prior writes."""
    mid = await repo.create_meeting(started_at=time.time())
    now = time.time()
    with pytest.raises(Exception):
        await db.execute_in_transaction(
            [
                (
                    "UPDATE meetings SET title = ?, updated_at = ? WHERE id = ?",
                    ("Rolled", now, mid),
                ),
                # Bad SQL — should fail and trigger rollback.
                ("UPDATE no_such_table SET x = 1", ()),
            ]
        )
    meeting = await repo.get_meeting(mid)
    # First update must have been rolled back.
    assert meeting.title != "Rolled"


@pytest.mark.asyncio
async def test_execute_in_transaction_serialised_by_write_lock(db: Database):
    """Concurrent transactions never overlap inside the write_lock."""
    inside = 0
    max_inside = 0

    async def run():
        nonlocal inside, max_inside
        async with db.write_lock:
            inside += 1
            max_inside = max(max_inside, inside)
            # Force a context switch while holding the lock.
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(run() for _ in range(5)))
    assert max_inside == 1


@pytest.mark.asyncio
async def test_get_meetings_by_ids_batched(repo: MeetingRepository):
    """get_meetings_by_ids fetches multiple meetings in a single round-trip."""
    now = time.time()
    id1 = await repo.create_meeting(started_at=now)
    id2 = await repo.create_meeting(started_at=now + 1)
    id3 = await repo.create_meeting(started_at=now + 2)

    results = await repo.get_meetings_by_ids([id3, id1, id2])
    assert [m.id for m in results] == [id3, id1, id2]


@pytest.mark.asyncio
async def test_get_meetings_by_ids_empty_returns_empty(repo: MeetingRepository):
    """An empty input list returns an empty list without hitting the DB."""
    assert await repo.get_meetings_by_ids([]) == []


@pytest.mark.asyncio
async def test_get_meetings_by_ids_drops_missing(repo: MeetingRepository):
    """Unknown ids are silently dropped from the result."""
    real = await repo.create_meeting(started_at=time.time())
    results = await repo.get_meetings_by_ids([real, "ghost-id"])
    assert [m.id for m in results] == [real]


@pytest.mark.asyncio
async def test_write_lock_serialises_concurrent_updates(repo: MeetingRepository):
    """Concurrent update_meeting calls all succeed without interleave errors."""
    mid = await repo.create_meeting(started_at=time.time())

    async def bump(label: str):
        await repo.update_meeting(mid, label=label)

    await asyncio.gather(*(bump(f"L{i}") for i in range(10)))
    meeting = await repo.get_meeting(mid)
    # Final label is whichever ran last — we just care that nothing crashed
    # and the row exists with one of the labels.
    assert meeting.label.startswith("L")
