"""Async CRUD for the calendar_events mirror table (Track B foundation)."""

import json
import logging
import time

from src.calendar_events.reader import CalendarEvent
from src.db.database import Database

logger = logging.getLogger("contextrecall.calendar_events")


class CalendarEventRepository:
    """Persisted rolling window of upcoming calendar events."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert(self, event: CalendarEvent) -> None:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO calendar_events "
                "(event_uid, title, start_ts, end_ts, attendees_json, organizer_json, "
                "join_url, meeting_id, calendar_name, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_uid) DO UPDATE SET "
                "title=excluded.title, start_ts=excluded.start_ts, end_ts=excluded.end_ts, "
                "attendees_json=excluded.attendees_json, organizer_json=excluded.organizer_json, "
                "join_url=excluded.join_url, meeting_id=excluded.meeting_id, "
                "calendar_name=excluded.calendar_name, synced_at=excluded.synced_at",
                (
                    event.event_uid,
                    event.title,
                    event.start_ts,
                    event.end_ts,
                    json.dumps(event.attendees or []),
                    json.dumps(event.organizer) if event.organizer else None,
                    event.join_url,
                    event.meeting_id,
                    event.calendar_name,
                    now,
                ),
            )
            await self._db.conn.commit()

    async def list_by_range(self, start: float, end: float) -> list[dict]:
        cur = await self._db.conn.execute(
            "SELECT * FROM calendar_events WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            (start, end),
        )
        return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def current_join_link_event(self, now: float, lead_seconds: float) -> dict | None:
        """Return the earliest join-link event whose armed window contains ``now``.

        Armed window is ``[start_ts - lead_seconds, end_ts]``. Only events with
        a non-empty ``join_url`` (virtual meetings) qualify. Deterministic on
        overlap via ``ORDER BY start_ts``. Read-only: no write_lock, no commit.
        """
        cur = await self._db.conn.execute(
            "SELECT * FROM calendar_events "
            "WHERE join_url != '' AND (start_ts - ?) <= ? AND end_ts >= ? "
            "ORDER BY start_ts LIMIT 1",
            (lead_seconds, now, now),
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def prune_window(self, start: float, end: float, keep_uids: set[str]) -> int:
        """Delete unrecorded window events absent from ``keep_uids``.

        A single DELETE under the write lock: the old SELECT-then-DELETE
        pair could race a concurrent upsert/recording-link between the two
        statements (harmless under today's single writer, but atomic is
        free here). ``keep_uids`` is bounded by the sync window (dozens),
        so inlining placeholders is safe.
        """
        placeholders = ",".join("?" for _ in keep_uids) or "''"
        async with self._db.write_lock:
            cur = await self._db.conn.execute(
                "DELETE FROM calendar_events "
                "WHERE start_ts >= ? AND start_ts < ? "
                "AND recorded_meeting_id IS NULL "
                f"AND event_uid NOT IN ({placeholders})",
                (start, end, *keep_uids),
            )
            await self._db.conn.commit()
        return cur.rowcount or 0

    async def set_recorded_meeting(self, event_uid: str, meeting_id: str | None) -> None:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE calendar_events SET recorded_meeting_id = ? WHERE event_uid = ?",
                (meeting_id, event_uid),
            )
            await self._db.conn.commit()

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["attendees"] = json.loads(d.pop("attendees_json") or "[]")
        except (ValueError, TypeError):
            # Coercing to [] keeps reads resilient, but corrupt JSON in the
            # mirror means a sync bug — surface it instead of hiding it.
            logger.warning(
                "Corrupt attendees_json on calendar event %s — coerced to []",
                d.get("event_uid"),
            )
            d["attendees"] = []
        try:
            d["organizer"] = (
                json.loads(d.pop("organizer_json")) if d.get("organizer_json") else None
            )
        except (ValueError, TypeError):
            logger.warning(
                "Corrupt organizer_json on calendar event %s — coerced to None",
                d.get("event_uid"),
            )
            d["organizer"] = None
        d.pop("organizer_json", None)
        return d
