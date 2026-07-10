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
        calendar_event_uid: str | None = None,
        event_signature: str | None = None,
    ) -> str:
        briefing_id = str(uuid.uuid4())
        now = time.time()
        if expires_at is None:
            expires_at = now + 7200  # 2 hours default TTL
        await self._db.conn.execute(
            """INSERT INTO prep_briefings
                (id, meeting_id, series_id, content_markdown, attendees_json,
                 related_meeting_ids_json, open_action_items_json, generated_at, expires_at,
                 calendar_event_uid, event_signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                calendar_event_uid,
                event_signature,
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

    async def has_current_for_event(self, calendar_event_uid: str, event_signature: str) -> bool:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT 1 FROM prep_briefings "
            "WHERE calendar_event_uid = ? AND event_signature = ? AND expires_at > ? LIMIT 1",
            (calendar_event_uid, event_signature, now),
        )
        return await cursor.fetchone() is not None

    async def get_by_calendar_event(self, calendar_event_uid: str) -> dict | None:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings "
            "WHERE calendar_event_uid = ? AND expires_at > ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (calendar_event_uid, now),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_upcoming(self, limit: int = 20) -> list[dict]:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM prep_briefings "
            "WHERE calendar_event_uid IS NOT NULL AND expires_at > ? "
            "ORDER BY generated_at DESC LIMIT ?",
            (now, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def prepared_event_uids(self) -> list[str]:
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT DISTINCT calendar_event_uid FROM prep_briefings "
            "WHERE calendar_event_uid IS NOT NULL AND expires_at > ?",
            (now,),
        )
        return [r[0] for r in await cursor.fetchall()]
