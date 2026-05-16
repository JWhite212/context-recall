"""
Data access layer for Context Recall meetings.

Provides async CRUD operations over the SQLite database.
"""

import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any

from src.db.database import Database

logger = logging.getLogger("contextrecall.db")

# Columns that update_meeting() is allowed to write.
_MUTABLE_COLUMNS = frozenset(
    {
        "title",
        "ended_at",
        "duration_seconds",
        "status",
        "audio_path",
        "transcript_json",
        "summary_markdown",
        "tags",
        "language",
        "word_count",
        "label",
        "calendar_event_title",
        "attendees_json",
        "calendar_confidence",
        "teams_join_url",
        "teams_meeting_id",
        "series_id",
        "updated_at",
    }
)


@dataclass
class MeetingRecord:
    """Flat representation of a meeting row for API serialisation."""

    id: str
    title: str
    started_at: float
    ended_at: float | None
    duration_seconds: float | None
    status: str
    audio_path: str | None
    transcript_json: str | None
    summary_markdown: str | None
    tags: list[str]
    language: str | None
    word_count: int | None
    created_at: float
    updated_at: float
    label: str = ""
    calendar_event_title: str = ""
    attendees_json: str = "[]"
    calendar_confidence: float = 0.0
    teams_join_url: str = ""
    teams_meeting_id: str = ""
    series_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "audio_path": self.audio_path,
            "transcript_json": self.transcript_json,
            "summary_markdown": self.summary_markdown,
            "tags": self.tags,
            "language": self.language,
            "word_count": self.word_count,
            "label": self.label,
            "calendar_event_title": self.calendar_event_title,
            "attendees_json": self.attendees_json,
            "calendar_confidence": self.calendar_confidence,
            "teams_join_url": self.teams_join_url,
            "teams_meeting_id": self.teams_meeting_id,
            "series_id": self.series_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> "MeetingRecord":
        tags_raw = row["tags"]
        tags = json.loads(tags_raw) if tags_raw else []

        # Calendar fields may not exist in older databases.
        calendar_event_title = ""
        attendees_json = "[]"
        calendar_confidence = 0.0
        teams_join_url = ""
        teams_meeting_id = ""
        try:
            calendar_event_title = row["calendar_event_title"] or ""
            attendees_json = row["attendees_json"] or "[]"
            calendar_confidence = row["calendar_confidence"] or 0.0
            teams_join_url = row["teams_join_url"] or ""
            teams_meeting_id = row["teams_meeting_id"] or ""
        except (IndexError, KeyError):
            pass

        series_id = None
        try:
            series_id = row["series_id"]
        except (IndexError, KeyError):
            pass

        return cls(
            id=row["id"],
            title=row["title"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            duration_seconds=row["duration_seconds"],
            status=row["status"],
            audio_path=row["audio_path"],
            transcript_json=row["transcript_json"],
            summary_markdown=row["summary_markdown"],
            tags=tags,
            language=row["language"],
            word_count=row["word_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            label=row["label"],
            calendar_event_title=calendar_event_title,
            attendees_json=attendees_json,
            calendar_confidence=calendar_confidence,
            teams_join_url=teams_join_url,
            teams_meeting_id=teams_meeting_id,
            series_id=series_id,
        )


class MeetingRepository:
    """Async data access for meetings."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_meeting(
        self,
        started_at: float,
        status: str = "recording",
    ) -> str:
        """Insert a new meeting and return its ID."""
        meeting_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """
                INSERT INTO meetings (id, started_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (meeting_id, started_at, status, now, now),
            )
            await self._db.conn.commit()
        logger.debug("Created meeting %s", meeting_id)
        return meeting_id

    async def update_meeting(self, meeting_id: str, **fields: Any) -> None:
        """Update one or more fields on a meeting."""
        if not fields:
            return

        invalid = set(fields) - _MUTABLE_COLUMNS
        if invalid:
            logger.warning(
                "update_meeting: rejected disallowed column(s) %s for meeting %s",
                invalid,
                meeting_id,
            )
            raise ValueError(f"Cannot update column(s): {invalid}")

        # Serialise tags as JSON.
        if "tags" in fields and isinstance(fields["tags"], list):
            fields["tags"] = json.dumps(fields["tags"])

        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        values = [v for _, v in pairs] + [meeting_id]

        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE meetings SET {set_clause} WHERE id = ?",
                values,
            )
            await self._db.conn.commit()

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        """Fetch a single meeting by ID."""
        cursor = await self._db.conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
        return MeetingRecord.from_row(row) if row else None

    async def get_meetings_by_ids(self, ids: list[str]) -> list[MeetingRecord]:
        """Batched fetch for many meetings in a single SELECT.

        Preserves the requested order via a dict-lookup so callers don't
        have to re-sort. Returns an empty list when ``ids`` is empty.
        Missing ids are simply omitted from the result.
        """
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        cursor = await self._db.conn.execute(
            f"SELECT * FROM meetings WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cursor.fetchall()
        by_id = {row["id"]: MeetingRecord.from_row(row) for row in rows}
        return [by_id[mid] for mid in ids if mid in by_id]

    _SORT_MAP = {
        "started_at:desc": "started_at DESC",
        "started_at:asc": "started_at ASC",
        "duration:desc": "duration_seconds DESC NULLS LAST, started_at DESC",
        "word_count:desc": "word_count DESC NULLS LAST, started_at DESC",
    }
    _SAFE_ORDERS = frozenset(_SORT_MAP.values()) | {"started_at DESC"}

    async def list_meetings(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        tag: str | None = None,
        sort: str | None = None,
    ) -> list[MeetingRecord]:
        """List meetings with optional status/tag filters and sort order."""
        conditions: list[str] = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE json_each.value = ?)")
            params.append(tag)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order = self._SORT_MAP.get(sort or "", "started_at DESC")
        assert order in self._SAFE_ORDERS, f"Unsafe sort order: {order}"
        params.extend([limit, offset])
        cursor = await self._db.conn.execute(
            f"SELECT * FROM meetings {where} ORDER BY {order} LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [MeetingRecord.from_row(r) for r in rows]

    async def list_meetings_by_date_range(
        self, start_ts: float, end_ts: float, limit: int = 1000
    ) -> list[MeetingRecord]:
        """List meetings whose started_at falls within [start_ts, end_ts)."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meetings"
            " WHERE started_at >= ? AND started_at < ?"
            " ORDER BY started_at ASC"
            " LIMIT ?",
            (start_ts, end_ts, limit),
        )
        rows = await cursor.fetchall()
        return [MeetingRecord.from_row(r) for r in rows]

    async def search_meetings(self, query: str, limit: int = 20) -> list[MeetingRecord]:
        """Full-text search across meeting title, summary, and transcript."""
        # FTS5 injection hardening: pass the query as a bound parameter and
        # double-up any embedded quotes per FTS5 escaping rules, then wrap in
        # double-quotes so the whole thing is treated as a phrase match (no
        # operator abuse: AND/OR/NOT/* etc).
        match_param = '"' + query.replace('"', '""') + '"'
        try:
            cursor = await self._db.conn.execute(
                """
                SELECT m.* FROM meetings m
                JOIN meetings_fts fts ON m.rowid = fts.rowid
                WHERE meetings_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_param, limit),
            )
            rows = await cursor.fetchall()
            return [MeetingRecord.from_row(r) for r in rows]
        except Exception:
            # FTS not available — fall back to LIKE search.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            cursor = await self._db.conn.execute(
                """
                SELECT * FROM meetings
                WHERE title LIKE ? ESCAPE '\\' OR summary_markdown LIKE ? ESCAPE '\\'
                ORDER BY started_at DESC LIMIT ?
                """,
                (like, like, limit),
            )
            rows = await cursor.fetchall()
            return [MeetingRecord.from_row(r) for r in rows]

    async def delete_meeting(self, meeting_id: str) -> bool:
        """Delete a meeting. Returns True if a row was deleted."""
        async with self._db.write_lock:
            cursor = await self._db.conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            await self._db.conn.commit()
            return cursor.rowcount > 0

    async def count_meetings(self, status: str | None = None, tag: str | None = None) -> int:
        """Count total meetings, optionally filtered by status and/or tag."""
        conditions: list[str] = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE json_each.value = ?)")
            params.append(tag)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.conn.execute(f"SELECT COUNT(*) FROM meetings {where}", params)
        row = await cursor.fetchone()
        return row[0]

    async def get_stats(self) -> dict:
        """Aggregate stats for the dashboard."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        start_of_week = (
            (now - datetime.timedelta(days=now.weekday()))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        cursor = await self._db.conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(duration_seconds), 0) / 3600.0,
                COALESCE(SUM(word_count), 0),
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0)
            FROM meetings
            """,
            (start_of_today, start_of_week),
        )
        row = await cursor.fetchone()
        return {
            "meetings_today": row[0],
            "meetings_this_week": row[1],
            "total_hours": round(row[2], 1),
            "total_words": row[3],
            "pending_count": row[4],
            "error_count": row[5],
        }

    async def get_distinct_labels(self) -> list[str]:
        """Return all unique non-empty labels, sorted alphabetically."""
        cursor = await self._db.conn.execute(
            "SELECT DISTINCT label FROM meetings WHERE label != '' ORDER BY label"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def cleanup_old_meetings(
        self, audio_retention_days: int, record_retention_days: int
    ) -> dict[str, int]:
        """Delete old audio files and/or meeting records based on retention policy.

        Returns counts of cleaned-up items.
        """
        now = time.time()
        audio_deleted = 0
        records_deleted = 0

        # Delete audio files older than audio_retention_days.
        if audio_retention_days > 0:
            cutoff = now - (audio_retention_days * 86400)
            cursor = await self._db.conn.execute(
                "SELECT id, audio_path FROM meetings "
                "WHERE audio_path IS NOT NULL AND started_at < ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            async with self._db.write_lock:
                for row in rows:
                    path = row["audio_path"]
                    if path and os.path.isfile(path):
                        try:
                            os.remove(path)
                            audio_deleted += 1
                        except OSError:
                            pass
                    await self._db.conn.execute(
                        "UPDATE meetings SET audio_path = NULL, updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                await self._db.conn.commit()
            if audio_deleted:
                logger.info("Retention: deleted %d audio file(s)", audio_deleted)

        # Delete entire meeting records older than record_retention_days.
        if record_retention_days > 0:
            cutoff = now - (record_retention_days * 86400)
            cursor = await self._db.conn.execute(
                "SELECT id, audio_path FROM meetings WHERE started_at < ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                path = row["audio_path"]
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            async with self._db.write_lock:
                await self._db.conn.execute("DELETE FROM meetings WHERE started_at < ?", (cutoff,))
                await self._db.conn.commit()
            records_deleted = len(rows)
            if records_deleted:
                logger.info("Retention: deleted %d meeting record(s)", records_deleted)

        return {"audio_deleted": audio_deleted, "records_deleted": records_deleted}

    async def reset_stale_inflight_meetings(self) -> int:
        """Flip any meeting still in a transient pipeline status to 'error'.

        Called on daemon startup. Any 'recording' / 'transcribing' row at
        startup is necessarily orphaned — the only thread that could have
        been advancing it died with the previous daemon process. Without
        this, such rows stay 'transcribing' forever and the UI surfaces no
        recovery action for them.
        """
        async with self._db.write_lock:
            cursor = await self._db.conn.execute(
                "UPDATE meetings SET status = 'error', updated_at = ? "
                "WHERE status IN ('recording', 'transcribing')",
                (time.time(),),
            )
            await self._db.conn.commit()
            return cursor.rowcount

    async def update_fts(self, meeting_id: str) -> None:
        """Update the FTS index for a meeting after transcript/summary changes."""
        try:
            meeting = await self.get_meeting(meeting_id)
            if not meeting:
                return

            transcript_text = ""
            if meeting.transcript_json:
                try:
                    data = json.loads(meeting.transcript_json)
                    segments = data.get("segments", [])
                    transcript_text = " ".join(s.get("text", "") for s in segments)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Delete old FTS entry then insert new one.
            async with self._db.write_lock:
                await self._db.conn.execute(
                    "DELETE FROM meetings_fts WHERE rowid = "
                    "(SELECT rowid FROM meetings WHERE id = ?)",
                    (meeting_id,),
                )
                await self._db.conn.execute(
                    """
                    INSERT INTO meetings_fts (rowid, title, summary_markdown, transcript_text)
                    SELECT rowid, title, summary_markdown, ?
                    FROM meetings WHERE id = ?
                    """,
                    (transcript_text, meeting_id),
                )
                await self._db.conn.commit()
        except Exception as e:
            if "no such table" in str(e).lower():
                logger.debug("FTS update skipped (FTS5 not available)")
            else:
                logger.warning("FTS update failed for meeting %s: %s", meeting_id, e)

    # ------------------------------------------------------------------
    # Embedding storage / retrieval for semantic search
    # ------------------------------------------------------------------

    async def store_embeddings(
        self,
        meeting_id: str,
        embeddings: list[dict],
    ) -> None:
        """Store segment embeddings for a meeting. Replaces existing embeddings.

        Each dict in *embeddings* must contain: segment_index, embedding,
        text, speaker (optional), and start_time.
        """
        from src.db.database import _vec_available

        async with self._db.write_lock:
            # Delete from vec0 first (before deleting from segment_embeddings).
            if _vec_available:
                try:
                    await self._db.conn.execute(
                        "DELETE FROM segment_embeddings_vec WHERE rowid IN "
                        "(SELECT id FROM segment_embeddings WHERE meeting_id = ?)",
                        (meeting_id,),
                    )
                except Exception:
                    pass

            # Delete existing embeddings for this meeting.
            await self._db.conn.execute(
                "DELETE FROM segment_embeddings WHERE meeting_id = ?", (meeting_id,)
            )
            for emb in embeddings:
                embedding_blob = struct.pack(f"{len(emb['embedding'])}f", *emb["embedding"])
                cursor = await self._db.conn.execute(
                    """INSERT INTO segment_embeddings
                       (meeting_id, segment_index, embedding, text, speaker, start_time)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        meeting_id,
                        emb["segment_index"],
                        embedding_blob,
                        emb["text"],
                        emb.get("speaker", ""),
                        emb["start_time"],
                    ),
                )
                if _vec_available:
                    rowid = cursor.lastrowid
                    try:
                        await self._db.conn.execute(
                            "INSERT INTO segment_embeddings_vec(rowid, embedding) VALUES (?, ?)",
                            (rowid, embedding_blob),
                        )
                    except Exception as e:
                        logger.warning("Failed to insert embedding into vec0: %s", e)
            await self._db.conn.commit()

    async def get_all_embeddings(self) -> list[dict]:
        """Retrieve all embeddings for search.

        Returns dicts with id, meeting_id, segment_index, embedding, text,
        speaker, and start_time.
        """
        cursor = await self._db.conn.execute(
            "SELECT id, meeting_id, segment_index, embedding, text, speaker, start_time "
            "FROM segment_embeddings LIMIT 100000"
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            blob = row["embedding"]
            num_floats = len(blob) // 4
            embedding = list(struct.unpack(f"{num_floats}f", blob))
            results.append(
                {
                    "id": row["id"],
                    "meeting_id": row["meeting_id"],
                    "segment_index": row["segment_index"],
                    "embedding": embedding,
                    "text": row["text"],
                    "speaker": row["speaker"],
                    "start_time": row["start_time"],
                }
            )
        return results

    async def search_embeddings(
        self,
        query_embedding: list[float],
        limit: int = 50,
        meeting_id: str | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
    ) -> list[dict]:
        """Search segment embeddings using sqlite-vec KNN.

        Falls back to brute-force if sqlite-vec is not available.
        """
        from src.db.database import _vec_available

        if not _vec_available:
            return await self._search_embeddings_bruteforce(
                query_embedding, limit, meeting_id, date_from, date_to
            )

        query_blob = struct.pack(f"{len(query_embedding)}f", *query_embedding)

        # Build query with optional filters.
        conditions: list[str] = []
        params: list = [query_blob, limit]

        base_sql = """
            SELECT se.id, se.meeting_id, se.segment_index, se.text, se.speaker, se.start_time,
                   v.distance
            FROM segment_embeddings_vec v
            JOIN segment_embeddings se ON se.id = v.rowid
            JOIN meetings m ON se.meeting_id = m.id
            WHERE v.embedding MATCH ? AND k = ?
        """

        if meeting_id:
            conditions.append("se.meeting_id = ?")
            params.append(meeting_id)
        if date_from is not None:
            conditions.append("m.started_at >= ?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("m.started_at <= ?")
            params.append(date_to)

        if conditions:
            base_sql += " AND " + " AND ".join(conditions)

        base_sql += " ORDER BY v.distance"

        cursor = await self._db.conn.execute(base_sql, params)
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "id": row["id"] if isinstance(row, dict) else row[0],
                    "meeting_id": row["meeting_id"] if isinstance(row, dict) else row[1],
                    "segment_index": row["segment_index"] if isinstance(row, dict) else row[2],
                    "text": row["text"] if isinstance(row, dict) else row[3],
                    "speaker": row["speaker"] if isinstance(row, dict) else row[4],
                    "start_time": row["start_time"] if isinstance(row, dict) else row[5],
                    "distance": row["distance"] if isinstance(row, dict) else row[6],
                }
            )
        return results

    async def _search_embeddings_bruteforce(
        self,
        query_embedding: list[float],
        limit: int = 50,
        meeting_id: str | None = None,
        date_from: float | None = None,
        date_to: float | None = None,
    ) -> list[dict]:
        """Brute-force fallback when sqlite-vec is not available."""
        import numpy as np

        conditions: list[str] = []
        params: list = []

        sql = """
            SELECT se.id, se.meeting_id, se.segment_index, se.embedding,
                   se.text, se.speaker, se.start_time
            FROM segment_embeddings se
            JOIN meetings m ON se.meeting_id = m.id
            WHERE 1=1
        """
        if meeting_id:
            conditions.append("se.meeting_id = ?")
            params.append(meeting_id)
        if date_from is not None:
            conditions.append("m.started_at >= ?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("m.started_at <= ?")
            params.append(date_to)

        if conditions:
            sql += " AND " + " AND ".join(conditions)
        sql += " LIMIT 100000"

        cursor = await self._db.conn.execute(sql, params)
        rows = await cursor.fetchall()

        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)
        if query_norm < 1e-10:
            return []

        scored = []
        for row in rows:
            blob = row["embedding"]
            num_floats = len(blob) // 4
            vec = np.array(struct.unpack(f"{num_floats}f", blob))
            vec_norm = np.linalg.norm(vec)
            if vec_norm < 1e-10:
                continue
            similarity = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
            scored.append(
                {
                    "id": row["id"],
                    "meeting_id": row["meeting_id"],
                    "segment_index": row["segment_index"],
                    "text": row["text"],
                    "speaker": row["speaker"],
                    "start_time": row["start_time"],
                    "distance": 1.0 - similarity,
                }
            )

        scored.sort(key=lambda x: x["distance"])
        return scored[:limit]

    async def search_hybrid(
        self,
        query_text: str,
        query_embedding: list[float],
        limit: int = 10,
        date_from: float | None = None,
        date_to: float | None = None,
    ) -> list[dict]:
        """Hybrid search combining FTS5 keyword results with vector results via RRF."""
        # 1. Vector search
        vec_results = await self.search_embeddings(
            query_embedding, limit=50, date_from=date_from, date_to=date_to
        )

        # 2. FTS5 keyword search
        fts_results: list[dict] = []
        try:
            # FTS5 injection hardening: bound parameter + doubled quotes.
            match_param = '"' + query_text.replace('"', '""') + '"'
            sql = """
                SELECT m.id as meeting_id, m.title, m.started_at,
                       rank as fts_rank
                FROM meetings_fts
                JOIN meetings m ON m.rowid = meetings_fts.rowid
                WHERE meetings_fts MATCH ?
            """
            params: list = [match_param]
            if date_from is not None:
                sql += " AND m.started_at >= ?"
                params.append(date_from)
            if date_to is not None:
                sql += " AND m.started_at <= ?"
                params.append(date_to)
            sql += " ORDER BY rank LIMIT 50"

            cursor = await self._db.conn.execute(sql, params)
            rows = await cursor.fetchall()
            for i, row in enumerate(rows):
                fts_results.append(
                    {
                        "meeting_id": row["meeting_id"],
                        "title": row["title"],
                        "started_at": row["started_at"],
                        "rank": i,
                    }
                )
        except Exception as e:
            logger.debug("FTS search failed: %s", e)

        # 3. Reciprocal Rank Fusion (RRF)
        rrf_k = 60  # RRF constant
        rrf_scores: dict[str, float] = {}
        result_data: dict[str, dict] = {}

        # Score vector results
        for i, r in enumerate(vec_results):
            key = f"{r['meeting_id']}:{r.get('segment_index', 0)}"
            rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (rrf_k + i)
            result_data[key] = r

        # Score FTS results -- meeting-level, boost all segments from matched meetings
        for i, r in enumerate(fts_results):
            meeting_boost = 1.0 / (rrf_k + i)
            # Boost any vector results from this meeting
            for key, data in result_data.items():
                if data["meeting_id"] == r["meeting_id"]:
                    rrf_scores[key] = rrf_scores.get(key, 0) + meeting_boost
            # Also add the meeting itself if no segments matched
            meeting_key = f"{r['meeting_id']}:fts"
            if meeting_key not in rrf_scores:
                rrf_scores[meeting_key] = meeting_boost
                result_data[meeting_key] = {
                    "meeting_id": r["meeting_id"],
                    "segment_index": 0,
                    "text": r.get("title", ""),
                    "speaker": "",
                    "start_time": 0.0,
                    "distance": 0.0,
                }

        # Sort by RRF score (descending)
        sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)

        results = []
        for key in sorted_keys[:limit]:
            data = result_data[key].copy()
            data["score"] = round(rrf_scores[key], 4)
            results.append(data)

        return results

    # ------------------------------------------------------------------
    # Speaker name mapping
    # ------------------------------------------------------------------

    async def set_speaker_name(
        self, meeting_id: str, speaker_id: str, display_name: str, source: str = "manual"
    ) -> None:
        """Set or update the display name for a speaker in a meeting.

        Also updates the transcript_json to replace speaker labels.
        """
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """INSERT INTO speaker_mappings
                   (meeting_id, speaker_id, display_name, source, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       source = excluded.source,
                       created_at = excluded.created_at""",
                (meeting_id, speaker_id, display_name, source, now),
            )
            await self._db.conn.commit()

        # Update transcript_json to replace speaker labels.
        meeting = await self.get_meeting(meeting_id)
        if meeting and meeting.transcript_json:
            try:
                data = json.loads(meeting.transcript_json)
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid transcript_json for meeting %s; skipping speaker rename",
                    meeting_id,
                )
                return
            for seg in data.get("segments", []):
                if seg.get("speaker") == speaker_id:
                    seg["speaker"] = display_name
            await self.update_meeting(meeting_id, transcript_json=json.dumps(data))

    async def get_speaker_names(self, meeting_id: str) -> list[dict]:
        """Get all speaker name mappings for a meeting."""
        cursor = await self._db.conn.execute(
            "SELECT speaker_id, display_name, source, created_at "
            "FROM speaker_mappings WHERE meeting_id = ? ORDER BY created_at",
            (meeting_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "speaker_id": row["speaker_id"],
                "display_name": row["display_name"],
                "source": row["source"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def get_global_speaker_names(self) -> list[dict]:
        """Get all unique speaker mappings across all meetings (most recent wins)."""
        cursor = await self._db.conn.execute(
            """SELECT sm.speaker_id, sm.display_name, sm.source, sm.created_at
               FROM speaker_mappings sm
               INNER JOIN (
                   SELECT speaker_id, MAX(created_at) as max_created
                   FROM speaker_mappings GROUP BY speaker_id
               ) latest ON sm.speaker_id = latest.speaker_id
                   AND sm.created_at = latest.max_created
               ORDER BY sm.display_name""",
        )
        rows = await cursor.fetchall()
        return [
            {
                "speaker_id": row["speaker_id"],
                "display_name": row["display_name"],
                "source": row["source"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def get_meeting_embeddings(self, meeting_id: str) -> list[dict]:
        """Retrieve embeddings for a specific meeting."""
        cursor = await self._db.conn.execute(
            "SELECT id, meeting_id, segment_index, embedding, text, speaker, start_time "
            "FROM segment_embeddings WHERE meeting_id = ?",
            (meeting_id,),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            blob = row["embedding"]
            num_floats = len(blob) // 4
            embedding = list(struct.unpack(f"{num_floats}f", blob))
            results.append(
                {
                    "id": row["id"],
                    "meeting_id": row["meeting_id"],
                    "segment_index": row["segment_index"],
                    "embedding": embedding,
                    "text": row["text"],
                    "speaker": row["speaker"],
                    "start_time": row["start_time"],
                }
            )
        return results

    # ------------------------------------------------------------------
    # Helper queries for analytics, series detection, and prep briefings
    # ------------------------------------------------------------------

    async def list_unlinked_complete_meetings(self) -> list[dict]:
        """Fetch complete meetings without a series_id."""
        cursor = await self._db.conn.execute(
            "SELECT id, title, started_at, duration_seconds, attendees_json "
            "FROM meetings "
            "WHERE status = 'complete' AND (series_id IS NULL OR series_id = '') "
            "ORDER BY started_at ASC"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_recent_complete_with_attendees(self, limit: int = 100) -> list[dict]:
        """Fetch recent complete meetings with attendee data."""
        cursor = await self._db.conn.execute(
            "SELECT id, title, started_at, summary_markdown, attendees_json "
            "FROM meetings WHERE status = 'complete' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_complete_in_range(self, start_ts: float, end_ts: float) -> list[dict]:
        """Fetch complete meetings in a timestamp range."""
        cursor = await self._db.conn.execute(
            "SELECT duration_seconds, word_count, attendees_json, "
            "series_id, started_at "
            "FROM meetings "
            "WHERE status = 'complete' AND started_at >= ? AND started_at < ?",
            (start_ts, end_ts),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def list_attendee_json_recent(self, limit: int = 200) -> list[dict]:
        """Fetch attendees_json from recent complete meetings."""
        cursor = await self._db.conn.execute(
            "SELECT attendees_json FROM meetings "
            "WHERE status = 'complete' AND attendees_json != '[]' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cursor.fetchall()]
