"""Tests for sqlite-vec extension loading (the semantic-search KNN path).

The extension must be loaded on aiosqlite's worker thread. sqlite3
connections are single-thread bound (check_same_thread), so touching the
raw connection from the caller's thread raises ProgrammingError — which
silently downgraded every deployed daemon to brute-force search since the
feature shipped ("sqlite-vec extension loaded successfully" appears zero
times in the full daemon log history).
"""

import sqlite3
import struct
import time

import pytest

import src.db.database as db_mod
from src.db.database import Database
from src.db.repository import MeetingRepository

sqlite_vec = pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason="sqlite3 built without loadable-extension support",
)

DIM = 384  # must match VEC_SQL's float[384]


def _embedding(seed: float) -> list[float]:
    return [seed] * DIM


def _rows(*seeds: float) -> list[dict]:
    return [
        {
            "segment_index": i,
            "embedding": _embedding(seed),
            "text": f"segment {i}",
            "speaker": "Me",
            "start_time": float(i),
        }
        for i, seed in enumerate(seeds)
    ]


@pytest.mark.asyncio
async def test_connect_loads_vec_extension(tmp_path):
    """connect() must actually load sqlite-vec (not trip the cross-thread
    guard) and create the vec0 table on a fresh install."""
    database = Database(db_path=tmp_path / "vec.db")
    await database.connect()
    try:
        assert db_mod._vec_available is True, (
            "sqlite-vec failed to load on connect() — the extension must be "
            "loaded via aiosqlite's async API so it runs on the worker thread"
        )
        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_embeddings_vec'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_vec_knn_search_used_when_available(tmp_path, monkeypatch):
    """With the extension loaded, search_embeddings must use the vec0 KNN
    path (not the brute-force fallback) and rank the nearest segment first."""
    import src.db.repository as repo_mod

    database = Database(db_path=tmp_path / "vec.db")
    await database.connect()
    try:
        assert db_mod._vec_available is True
        repo = MeetingRepository(database)
        mid = await repo.create_meeting(started_at=time.time())
        await repo.store_embeddings(mid, _rows(0.1, 0.9))

        called = {"n": 0}
        original_bf = repo_mod.MeetingRepository._search_embeddings_bruteforce

        async def spy_bf(self, *args, **kwargs):
            called["n"] += 1
            return await original_bf(self, *args, **kwargs)

        monkeypatch.setattr(repo_mod.MeetingRepository, "_search_embeddings_bruteforce", spy_bf)

        results = await repo.search_embeddings(_embedding(0.88), limit=2)

        assert called["n"] == 0, "vec0 KNN path must be used when available"
        assert len(results) == 2
        assert results[0]["segment_index"] == 1  # 0.9-vector is nearest to 0.88
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_vec_table_backfilled_for_db_migrated_without_vec(tmp_path):
    """A database migrated while sqlite-vec was unavailable (every deployed
    DB before the fix) must gain the vec0 table — backfilled from
    segment_embeddings — on the next successful connect().

    The v4→v5 migration gates vec0 creation on _vec_available and never
    re-runs once user_version has advanced, so connect() has to repair this
    idempotently.
    """

    async def failing_loader(conn):
        db_mod._vec_available = False
        return False

    original_loader = db_mod._load_vec_extension
    db_mod._load_vec_extension = failing_loader
    try:
        database = Database(db_path=tmp_path / "legacy.db")
        await database.connect()
        repo = MeetingRepository(database)
        mid = await repo.create_meeting(started_at=time.time())
        await repo.store_embeddings(mid, _rows(0.2, 0.4, 0.6))

        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_embeddings_vec'"
        )
        assert await cursor.fetchone() is None, "precondition: legacy DB must lack the vec0 table"
        await database.close()
    finally:
        db_mod._load_vec_extension = original_loader

    # Second boot with a working loader: table created and backfilled.
    database = Database(db_path=tmp_path / "legacy.db")
    await database.connect()
    try:
        assert db_mod._vec_available is True
        cursor = await database.conn.execute(
            "SELECT v.rowid, v.embedding FROM segment_embeddings_vec v ORDER BY v.rowid"
        )
        vec_rows = await cursor.fetchall()
        cursor = await database.conn.execute(
            "SELECT id, embedding FROM segment_embeddings ORDER BY id"
        )
        base_rows = await cursor.fetchall()
        assert len(base_rows) == 3
        assert [r[0] for r in vec_rows] == [r[0] for r in base_rows]
        # Spot-check blob content round-tripped intact.
        assert struct.unpack(f"{DIM}f", vec_rows[0][1])[0] == pytest.approx(0.2)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_knn_failure_falls_back_to_bruteforce(tmp_path):
    """A vec0 query error (e.g. dimension mismatch) must degrade to the
    brute-force path with a warning, never crash the search."""
    database = Database(db_path=tmp_path / "vec.db")
    await database.connect()
    try:
        assert db_mod._vec_available is True
        repo = MeetingRepository(database)
        mid = await repo.create_meeting(started_at=time.time())
        # 3-dim embeddings: the vec0 mirror insert fails (warned), rows only
        # exist in segment_embeddings.
        await repo.store_embeddings(
            mid,
            [
                {
                    "segment_index": 0,
                    "embedding": [0.1, 0.2, 0.3],
                    "text": "tiny",
                    "speaker": "Me",
                    "start_time": 0.0,
                }
            ],
        )

        # 3-dim query vector: the KNN MATCH raises OperationalError inside
        # sqlite-vec; search must fall back instead of propagating it.
        results = await repo.search_embeddings([0.1, 0.2, 0.3], limit=5)

        assert len(results) == 1
        assert results[0]["text"] == "tiny"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_backfill_is_idempotent(tmp_path):
    """Reconnecting must not duplicate vec0 rows for already-mirrored
    embeddings."""
    database = Database(db_path=tmp_path / "vec.db")
    await database.connect()
    repo = MeetingRepository(database)
    mid = await repo.create_meeting(started_at=time.time())
    await repo.store_embeddings(mid, _rows(0.3, 0.7))
    await database.close()

    database = Database(db_path=tmp_path / "vec.db")
    await database.connect()
    try:
        cursor = await database.conn.execute("SELECT count(*) FROM segment_embeddings_vec")
        row = await cursor.fetchone()
        assert row[0] == 2
    finally:
        await database.close()
