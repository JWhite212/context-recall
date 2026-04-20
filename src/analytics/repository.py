"""Data access for pre-computed analytics."""

import time

from src.db.database import Database


class AnalyticsRepository:
    """Async CRUD for meeting_analytics table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, period_type: str, period_start: str, **metrics) -> None:
        """Insert or update analytics for a period."""
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT id FROM meeting_analytics WHERE period_type = ? AND period_start = ?",
            (period_type, period_start),
        )
        existing = await cursor.fetchone()
        if existing:
            fields = {**metrics, "computed_at": now}
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [existing[0]]
            await self._db.conn.execute(
                f"UPDATE meeting_analytics SET {set_clause} WHERE id = ?", values
            )
        else:
            await self._db.conn.execute(
                """INSERT INTO meeting_analytics
                    (period_type, period_start, total_meetings, total_duration_minutes,
                     total_words, unique_attendees, recurring_ratio,
                     action_items_created, action_items_completed, busiest_hour, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    period_type,
                    period_start,
                    metrics.get("total_meetings", 0),
                    metrics.get("total_duration_minutes", 0),
                    metrics.get("total_words", 0),
                    metrics.get("unique_attendees", 0),
                    metrics.get("recurring_ratio", 0.0),
                    metrics.get("action_items_created", 0),
                    metrics.get("action_items_completed", 0),
                    metrics.get("busiest_hour"),
                    now,
                ),
            )
        await self._db.conn.commit()

    async def get_period(self, period_type: str, period_start: str) -> dict | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM meeting_analytics WHERE period_type = ? AND period_start = ?",
            (period_type, period_start),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_range(self, period_type: str, start: str, end: str) -> list[dict]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM meeting_analytics "
            "WHERE period_type = ? AND period_start >= ? AND period_start <= ? "
            "ORDER BY period_start ASC",
            (period_type, start, end),
        )
        return [dict(row) for row in await cursor.fetchall()]
