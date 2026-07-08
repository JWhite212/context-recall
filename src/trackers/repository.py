"""Data access for keyword trackers and their hits."""

import json
import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.trackers")


class TrackerRepository:
    """Async CRUD for trackers + tracker_hits."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, name: str, keywords: list[str], enabled: bool = True) -> str:
        tracker_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """INSERT INTO trackers (id, name, keywords_json, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tracker_id, name, json.dumps(keywords), 1 if enabled else 0, now, now),
            )
            await self._db.conn.commit()
        return tracker_id

    async def update(
        self,
        tracker_id: str,
        *,
        name: str | None = None,
        keywords: list[str] | None = None,
        enabled: bool | None = None,
    ) -> None:
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if keywords is not None:
            fields["keywords_json"] = json.dumps(keywords)
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        if not fields:
            return
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE trackers SET {set_clause} WHERE id = ?",
                [v for _, v in pairs] + [tracker_id],
            )
            await self._db.conn.commit()

    async def get(self, tracker_id: str) -> dict | None:
        cursor = await self._db.conn.execute("SELECT * FROM trackers WHERE id = ?", (tracker_id,))
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_trackers(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        cursor = await self._db.conn.execute(
            f"SELECT * FROM trackers {where} ORDER BY name COLLATE NOCASE"
        )
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def delete(self, tracker_id: str) -> bool:
        async with self._db.write_lock:
            cursor = await self._db.conn.execute("DELETE FROM trackers WHERE id = ?", (tracker_id,))
            await self._db.conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["keywords"] = json.loads(d.pop("keywords_json") or "[]")
        except (ValueError, TypeError):
            d["keywords"] = []
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    # ------------------------------------------------------------------
    # Hits
    # ------------------------------------------------------------------

    async def replace_hits_for_meeting(self, meeting_id: str, hits: list[dict]) -> int:
        """Replace a meeting's hits (reprocess-safe). Returns count stored."""
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "DELETE FROM tracker_hits WHERE meeting_id = ?", (meeting_id,)
            )
            for hit in hits:
                await self._db.conn.execute(
                    """INSERT INTO tracker_hits
                       (tracker_id, meeting_id, segment_index, matched_keyword,
                        matched_text, start_time, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        hit["tracker_id"],
                        meeting_id,
                        hit["segment_index"],
                        hit["matched_keyword"],
                        hit.get("matched_text", ""),
                        hit.get("start_time", 0.0),
                        now,
                    ),
                )
            await self._db.conn.commit()
        return len(hits)

    async def hits_for_meeting(self, meeting_id: str) -> list[dict]:
        cursor = await self._db.conn.execute(
            """SELECT h.*, t.name AS tracker_name FROM tracker_hits h
               JOIN trackers t ON t.id = h.tracker_id
               WHERE h.meeting_id = ? ORDER BY h.start_time""",
            (meeting_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def hits_for_tracker(self, tracker_id: str, limit: int = 200) -> list[dict]:
        cursor = await self._db.conn.execute(
            """SELECT h.*, m.title AS meeting_title, m.started_at AS meeting_started_at
               FROM tracker_hits h
               JOIN meetings m ON m.id = h.meeting_id
               WHERE h.tracker_id = ?
               ORDER BY m.started_at DESC, h.start_time
               LIMIT ?""",
            (tracker_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]
