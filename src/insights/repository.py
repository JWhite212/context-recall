"""Data access for custom insight definitions and their per-meeting results."""

import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.insights")


class InsightRepository:
    """Async CRUD for insight_definitions + insight_results.

    Results carry a denormalised ``definition_name`` so they survive a
    definition rename or delete (there is no FK cascade on definition_id).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, name: str, prompt: str, enabled: bool = True) -> str:
        insight_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO insight_definitions "
                "(id, name, prompt, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (insight_id, name, prompt, 1 if enabled else 0, now, now),
            )
            await self._db.conn.commit()
        return insight_id

    async def update(self, insight_id, *, name=None, prompt=None, enabled=None) -> None:
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if prompt is not None:
            sets.append("prompt = ?")
            vals.append(prompt)
        if enabled is not None:
            sets.append("enabled = ?")
            vals.append(1 if enabled else 0)
        if not sets:
            return
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(insight_id)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE insight_definitions SET {', '.join(sets)} WHERE id = ?", vals
            )
            await self._db.conn.commit()

    async def get(self, insight_id: str) -> dict | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM insight_definitions WHERE id = ?", (insight_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_definitions(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM insight_definitions"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at"
        cursor = await self._db.conn.execute(sql)
        return [self._row_to_dict(r) for r in await cursor.fetchall()]

    async def delete(self, insight_id: str) -> bool:
        async with self._db.write_lock:
            cursor = await self._db.conn.execute(
                "DELETE FROM insight_definitions WHERE id = ?", (insight_id,)
            )
            await self._db.conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def replace_results_for_meeting(self, meeting_id: str, results: list[dict]) -> int:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "DELETE FROM insight_results WHERE meeting_id = ?", (meeting_id,)
            )
            for r in results:
                await self._db.conn.execute(
                    "INSERT INTO insight_results "
                    "(definition_id, definition_name, meeting_id, content, speaker, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        r["definition_id"],
                        r["definition_name"],
                        meeting_id,
                        r["content"],
                        r.get("speaker", ""),
                        now,
                    ),
                )
            await self._db.conn.commit()
        return len(results)

    async def results_for_meeting(self, meeting_id: str) -> list[dict]:
        cursor = await self._db.conn.execute(
            "SELECT definition_id, definition_name, content, speaker "
            "FROM insight_results WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        )
        return [
            {
                "definition_id": r["definition_id"],
                "definition_name": r["definition_name"],
                "content": r["content"],
                "speaker": r["speaker"],
            }
            for r in await cursor.fetchall()
        ]
