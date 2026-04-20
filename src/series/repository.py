"""
Data access layer for meeting series.

Provides async CRUD operations for recurring meeting series,
including linking/unlinking meetings and calendar-based lookups.
"""

import logging
import uuid
from datetime import datetime, timezone

from src.db.database import Database

logger = logging.getLogger("meetingmind.series")

_MUTABLE_FIELDS = frozenset(
    {
        "title",
        "calendar_series_id",
        "detection_method",
        "typical_attendees_json",
        "typical_day_of_week",
        "typical_time",
        "typical_duration_minutes",
        "updated_at",
    }
)


class SeriesRepository:
    """Async data access for meeting series."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        title: str,
        detection_method: str,
        calendar_series_id: str | None = None,
        typical_attendees_json: str | None = None,
        typical_day_of_week: int | None = None,
        typical_time: str | None = None,
        typical_duration_minutes: int | None = None,
    ) -> str:
        """Insert a new meeting series and return its ID."""
        series_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            """
            INSERT INTO meeting_series
                (id, title, calendar_series_id, detection_method,
                 typical_attendees_json, typical_day_of_week,
                 typical_time, typical_duration_minutes,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                series_id,
                title,
                calendar_series_id,
                detection_method,
                typical_attendees_json,
                typical_day_of_week,
                typical_time,
                typical_duration_minutes,
                now,
                now,
            ),
        )
        await self._db.conn.commit()
        logger.debug("Created series %s (%s)", series_id, title)
        return series_id

    async def get(self, series_id: str) -> dict | None:
        """Fetch a single meeting series by ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meeting_series WHERE id = ?", (series_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_all(self) -> list[dict]:
        """List all meeting series, most recently updated first."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meeting_series ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update(self, series_id: str, **fields) -> None:
        """Update one or more fields on a meeting series."""
        if not fields:
            return
        fields = {k: v for k, v in fields.items() if k in _MUTABLE_FIELDS}
        if not fields:
            return
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        values = [v for _, v in pairs] + [series_id]
        await self._db.conn.execute(
            f"UPDATE meeting_series SET {set_clause} WHERE id = ?",
            values,
        )
        await self._db.conn.commit()

    async def delete(self, series_id: str) -> None:
        """Delete a meeting series, unlinking any associated meetings first."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            "UPDATE meetings SET series_id = NULL, updated_at = ? WHERE series_id = ?",
            (now, series_id),
        )
        await self._db.conn.execute("DELETE FROM meeting_series WHERE id = ?", (series_id,))
        await self._db.conn.commit()

    async def link_meeting(self, meeting_id: str, series_id: str) -> None:
        """Associate a meeting with a series."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            "UPDATE meetings SET series_id = ?, updated_at = ? WHERE id = ?",
            (series_id, now, meeting_id),
        )
        await self._db.conn.commit()

    async def unlink_meeting(self, meeting_id: str) -> None:
        """Remove the series association from a meeting."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.conn.execute(
            "UPDATE meetings SET series_id = NULL, updated_at = ? WHERE id = ?",
            (now, meeting_id),
        )
        await self._db.conn.commit()

    async def get_meetings(self, series_id: str) -> list[dict]:
        """Fetch all meetings linked to a series, most recent first."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meetings WHERE series_id = ? ORDER BY started_at DESC",
            (series_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def find_by_calendar_id(self, calendar_series_id: str) -> dict | None:
        """Look up a meeting series by its calendar series ID."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM meeting_series WHERE calendar_series_id = ?",
            (calendar_series_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
