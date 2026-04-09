"""
Data access layer for MeetingMind meetings.

Provides async CRUD operations over the SQLite database.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from src.db.database import Database

logger = logging.getLogger("meetingmind.db")

# Columns that update_meeting() is allowed to write.
_MUTABLE_COLUMNS = frozenset({
    "title", "ended_at", "duration_seconds", "status",
    "audio_path", "transcript_json", "summary_markdown",
    "tags", "language", "word_count",
})


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
            raise ValueError(f"Cannot update column(s): {invalid}")

        # Serialise tags as JSON.
        if "tags" in fields and isinstance(fields["tags"], list):
            fields["tags"] = json.dumps(fields["tags"])

        fields["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [meeting_id]

        await self._db.conn.execute(
            f"UPDATE meetings SET {set_clause} WHERE id = ?",
            values,
        )
        await self._db.conn.commit()

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        """Fetch a single meeting by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        return MeetingRecord.from_row(row) if row else None

    async def list_meetings(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> list[MeetingRecord]:
        """List meetings, newest first."""
        if status:
            cursor = await self._db.conn.execute(
                "SELECT * FROM meetings WHERE status = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor = await self._db.conn.execute(
                "SELECT * FROM meetings ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [MeetingRecord.from_row(r) for r in rows]

    async def search_meetings(self, query: str, limit: int = 20) -> list[MeetingRecord]:
        """Full-text search across meeting title, summary, and transcript."""
        try:
            cursor = await self._db.conn.execute(
                """
                SELECT m.* FROM meetings m
                JOIN meetings_fts fts ON m.rowid = fts.rowid
                WHERE meetings_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            )
            rows = await cursor.fetchall()
            return [MeetingRecord.from_row(r) for r in rows]
        except Exception:
            # FTS not available — fall back to LIKE search.
            like = f"%{query}%"
            cursor = await self._db.conn.execute(
                """
                SELECT * FROM meetings
                WHERE title LIKE ? OR summary_markdown LIKE ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (like, like, limit),
            )
            rows = await cursor.fetchall()
            return [MeetingRecord.from_row(r) for r in rows]

    async def delete_meeting(self, meeting_id: str) -> bool:
        """Delete a meeting. Returns True if a row was deleted."""
        cursor = await self._db.conn.execute(
            "DELETE FROM meetings WHERE id = ?", (meeting_id,)
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def count_meetings(self, status: str | None = None) -> int:
        """Count total meetings, optionally filtered by status."""
        if status:
            cursor = await self._db.conn.execute(
                "SELECT COUNT(*) FROM meetings WHERE status = ?", (status,)
            )
        else:
            cursor = await self._db.conn.execute("SELECT COUNT(*) FROM meetings")
        row = await cursor.fetchone()
        return row[0]

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
                "SELECT id, audio_path FROM meetings WHERE audio_path IS NOT NULL AND started_at < ?",
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
            await self._db.conn.execute(
                "DELETE FROM meetings WHERE started_at < ?", (cutoff,)
            )
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
