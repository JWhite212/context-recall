"""Async CRUD for the notifications table."""

import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("meetingmind.notifications.repo")


class NotificationRepository:
    """Data access layer for notification records."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        type: str,
        title: str,
        body: str,
        channel: str,
        reference_id: str | None = None,
        status: str = "sent",
        scheduled_at: float | None = None,
    ) -> str:
        """Insert a notification row and return its ID."""
        notif_id = str(uuid.uuid4())
        now = time.time()
        sent_at = now if status == "sent" else None
        await self._db.conn.execute(
            """
            INSERT INTO notifications
                (id, type, reference_id, channel, title, body,
                 status, scheduled_at, sent_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notif_id,
                type,
                reference_id,
                channel,
                title,
                body,
                status,
                scheduled_at,
                sent_at,
                now,
            ),
        )
        await self._db.conn.commit()
        logger.debug("Created notification %s (type=%s, channel=%s)", notif_id, type, channel)
        return notif_id

    async def find_recent(
        self,
        type: str,
        reference_id: str | None,
        channel: str,
        window_minutes: int = 60,
    ) -> bool:
        """Check if a matching notification was sent within the time window (for deduplication)."""
        cutoff = time.time() - (window_minutes * 60)
        if reference_id is not None:
            cursor = await self._db.conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE type = ? AND reference_id = ? AND channel = ? AND created_at >= ?
                LIMIT 1
                """,
                (type, reference_id, channel, cutoff),
            )
        else:
            cursor = await self._db.conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE type = ? AND reference_id IS NULL AND channel = ? AND created_at >= ?
                LIMIT 1
                """,
                (type, channel, cutoff),
            )
        row = await cursor.fetchone()
        return row is not None

    async def list_notifications(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict]:
        """Query notifications with optional status filter, ordered by created_at DESC."""
        if status is not None:
            cursor = await self._db.conn.execute(
                """
                SELECT id, type, reference_id, channel, title, body, status,
                       scheduled_at, sent_at, created_at
                FROM notifications
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset),
            )
        else:
            cursor = await self._db.conn.execute(
                """
                SELECT id, type, reference_id, channel, title, body, status,
                       scheduled_at, sent_at, created_at
                FROM notifications
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "reference_id": r["reference_id"],
                "channel": r["channel"],
                "title": r["title"],
                "body": r["body"],
                "status": r["status"],
                "scheduled_at": r["scheduled_at"],
                "sent_at": r["sent_at"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def count_unread(self) -> int:
        """Count notifications with status='sent' (i.e. not yet dismissed)."""
        cursor = await self._db.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE status = 'sent'"
        )
        row = await cursor.fetchone()
        return row[0]

    async def dismiss(self, notif_id: str, status: str = "dismissed") -> None:
        """Update a notification's status."""
        await self._db.conn.execute(
            "UPDATE notifications SET status = ? WHERE id = ?",
            (status, notif_id),
        )
        await self._db.conn.commit()
