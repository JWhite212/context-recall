"""Data access for prep briefings."""

import time
import uuid

from src.db.database import Database


class PrepRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        content_markdown: str,
        attendees_json: str = "[]",
        series_id: str | None = None,
        meeting_id: str | None = None,
        related_meeting_ids_json: str = "[]",
        open_action_items_json: str = "[]",
        expires_at: float | None = None,
    ) -> str:
        briefing_id = str(uuid.uuid4())
        now = time.time()
        if expires_at is None:
            expires_at = now + 7200  # 2 hours default TTL
        await self._db.conn.execute(
            """INSERT INTO prep_briefings
                (id, meeting_id, series_id, content_markdown, attendees_json,
                 related_meeting_ids_json, open_action_items_json, generated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                briefing_id,
                meeting_id,
                series_id,
                content_markdown,
                attendees_json,
                related_meeting_ids_json,
                open_action_items_json,
                now,
                expires_at,
            ),
        )
        await self._db.conn.commit()
        return briefing_id

    async def get(self, briefing_id: str) -> dict | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings WHERE id = ?", (briefing_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_upcoming(self) -> dict | None:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings WHERE expires_at > ? ORDER BY generated_at DESC LIMIT 1",
            (now,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_by_meeting(self, meeting_id: str) -> dict | None:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings "
            "WHERE meeting_id = ? AND expires_at > ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (meeting_id, now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
