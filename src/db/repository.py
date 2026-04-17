"""
Data access layer for MeetingMind meetings.

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

logger = logging.getLogger("meetingmind.db")

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
        "updated_at",
    }
)

# Module-level cache for get_all_embeddings (invalidated by store_embeddings).
_embedding_cache: list[dict] | None = None


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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> "MeetingRecord":
        tags_raw = row["tags"]
        tags = json.loads(tags_raw) if tags_raw else []
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
        )


class MeetingRepository:
    """Async data access for meetings."""

    def __init__(self, db: Database) -> None:
        global _embedding_cache
        _embedding_cache = None
        self._db = db

    async def create_meeting(
        self,
        started_at: float,
        status: str = "recording",
    ) -> str:
        """Insert a new meeting and return its ID."""
        meeting_id = str(uuid.uuid4())
        now = time.time()
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

    async def search_meetings(self, query: str, limit: int = 20) -> list[MeetingRecord]:
        """Full-text search across meeting title, summary, and transcript."""
        # Wrap the query in double-quotes to treat it as a phrase search and
        # prevent FTS5 operator abuse (AND/OR/NOT/* etc).
        safe_query = '"' + query.replace('"', "") + '"'
        try:
            cursor = await self._db.conn.execute(
                """
                SELECT m.* FROM meetings m
                JOIN meetings_fts fts ON m.rowid = fts.rowid
                WHERE meetings_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
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
            await self._db.conn.execute("DELETE FROM meetings WHERE started_at < ?", (cutoff,))
            await self._db.conn.commit()
            records_deleted = len(rows)
            if records_deleted:
                logger.info("Retention: deleted %d meeting record(s)", records_deleted)

        return {"audio_deleted": audio_deleted, "records_deleted": records_deleted}

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
            await self._db.conn.execute(
                "DELETE FROM meetings_fts WHERE rowid = (SELECT rowid FROM meetings WHERE id = ?)",
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
        global _embedding_cache
        _embedding_cache = None
        # Delete existing embeddings for this meeting first.
        await self._db.conn.execute(
            "DELETE FROM segment_embeddings WHERE meeting_id = ?", (meeting_id,)
        )
        for emb in embeddings:
            embedding_blob = struct.pack(f"{len(emb['embedding'])}f", *emb["embedding"])
            await self._db.conn.execute(
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
        await self._db.conn.commit()

    async def get_all_embeddings(self) -> list[dict]:
        """Retrieve all embeddings for search.

        Returns dicts with id, meeting_id, segment_index, embedding, text,
        speaker, and start_time.
        """
        global _embedding_cache
        if _embedding_cache is not None:
            return _embedding_cache
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
        _embedding_cache = results
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
