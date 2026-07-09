"""Data access for automation rules and their per-meeting dispatch records."""

import json
import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.automations")


class AutomationRepository:
    """Async CRUD for automation_rules + automation_dispatches."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        name: str,
        match_mode: str = "all",
        conditions: list | None = None,
        actions: list | None = None,
        enabled: bool = True,
    ) -> str:
        rule_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO automation_rules "
                "(id, name, enabled, match_mode, conditions_json, actions_json, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_id,
                    name,
                    1 if enabled else 0,
                    match_mode,
                    json.dumps(conditions or []),
                    json.dumps(actions or []),
                    now,
                    now,
                ),
            )
            await self._db.conn.commit()
        return rule_id

    async def update(
        self,
        rule_id: str,
        *,
        name=None,
        match_mode=None,
        conditions=None,
        actions=None,
        enabled=None,
    ) -> None:
        fields: dict = {}
        if name is not None:
            fields["name"] = name
        if match_mode is not None:
            fields["match_mode"] = match_mode
        if conditions is not None:
            fields["conditions_json"] = json.dumps(conditions)
        if actions is not None:
            fields["actions_json"] = json.dumps(actions)
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        if not fields:
            return
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE automation_rules SET {set_clause} WHERE id = ?",
                [v for _, v in pairs] + [rule_id],
            )
            await self._db.conn.commit()

    async def get(self, rule_id: str) -> dict | None:
        cur = await self._db.conn.execute("SELECT * FROM automation_rules WHERE id = ?", (rule_id,))
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_rules(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        cur = await self._db.conn.execute(
            f"SELECT * FROM automation_rules {where} ORDER BY created_at"
        )
        return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def delete(self, rule_id: str) -> bool:
        async with self._db.write_lock:
            cur = await self._db.conn.execute(
                "DELETE FROM automation_rules WHERE id = ?", (rule_id,)
            )
            await self._db.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["conditions"] = json.loads(d.pop("conditions_json") or "[]")
        except (ValueError, TypeError):
            d["conditions"] = []
        try:
            d["actions"] = json.loads(d.pop("actions_json") or "[]")
        except (ValueError, TypeError):
            d["actions"] = []
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    # ------------------------------------------------------------------
    # Dispatches
    # ------------------------------------------------------------------

    async def has_dispatched(self, rule_id: str, meeting_id: str) -> bool:
        cur = await self._db.conn.execute(
            "SELECT 1 FROM automation_dispatches WHERE rule_id = ? AND meeting_id = ?",
            (rule_id, meeting_id),
        )
        return await cur.fetchone() is not None

    async def record_dispatch(self, rule_id: str, meeting_id: str) -> None:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT OR IGNORE INTO automation_dispatches "
                "(rule_id, meeting_id, created_at) VALUES (?, ?, ?)",
                (rule_id, meeting_id, time.time()),
            )
            await self._db.conn.commit()

    async def fired_rules_for_meeting(self, meeting_id: str) -> list[dict]:
        cur = await self._db.conn.execute(
            "SELECT r.id AS id, r.name AS name FROM automation_dispatches d "
            "JOIN automation_rules r ON r.id = d.rule_id "
            "WHERE d.meeting_id = ? ORDER BY d.created_at",
            (meeting_id,),
        )
        return [{"id": r["id"], "name": r["name"]} for r in await cur.fetchall()]
