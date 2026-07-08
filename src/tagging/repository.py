"""Data access for clients and projects."""

import json
import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.tagging")

_CLIENT_COLUMNS = frozenset({"name", "description", "aliases_json", "email_domains_json", "status"})
_PROJECT_COLUMNS = frozenset({"client_id", "name", "description", "aliases_json", "status"})


def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ("aliases_json", "email_domains_json"):
        if key in d:
            try:
                d[key.removesuffix("_json")] = json.loads(d.pop(key) or "[]")
            except (ValueError, TypeError):
                d[key.removesuffix("_json")] = []
    return d


class ClientProjectRepository:
    """Async CRUD for the clients and projects tables."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Clients
    # ------------------------------------------------------------------

    async def create_client(
        self,
        name: str,
        description: str = "",
        aliases: list[str] | None = None,
        email_domains: list[str] | None = None,
    ) -> str:
        client_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """INSERT INTO clients
                   (id, name, description, aliases_json, email_domains_json,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    client_id,
                    name,
                    description,
                    json.dumps(aliases or []),
                    json.dumps([d.lower().lstrip("@") for d in (email_domains or [])]),
                    now,
                    now,
                ),
            )
            await self._db.conn.commit()
        return client_id

    async def update_client(self, client_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields) - _CLIENT_COLUMNS
        if invalid:
            raise ValueError(f"Cannot update column(s): {invalid}")
        for key in ("aliases_json", "email_domains_json"):
            if key in fields and isinstance(fields[key], list):
                values = fields[key]
                if key == "email_domains_json":
                    values = [d.lower().lstrip("@") for d in values]
                fields[key] = json.dumps(values)
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE clients SET {set_clause} WHERE id = ?",
                [v for _, v in pairs] + [client_id],
            )
            await self._db.conn.commit()

    async def get_client(self, client_id: str) -> dict | None:
        cursor = await self._db.conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def list_clients(self, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE status = 'active'"
        cursor = await self._db.conn.execute(
            f"SELECT * FROM clients {where} ORDER BY name COLLATE NOCASE"
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def delete_client(self, client_id: str) -> bool:
        """Delete a client; its projects survive unlinked, meetings unassign."""
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE meetings SET client_id = NULL, updated_at = ? WHERE client_id = ?",
                (time.time(), client_id),
            )
            cursor = await self._db.conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
            await self._db.conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    async def create_project(
        self,
        name: str,
        client_id: str | None = None,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> str:
        project_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """INSERT INTO projects
                   (id, client_id, name, description, aliases_json, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (project_id, client_id, name, description, json.dumps(aliases or []), now, now),
            )
            await self._db.conn.commit()
        return project_id

    async def update_project(self, project_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields) - _PROJECT_COLUMNS
        if invalid:
            raise ValueError(f"Cannot update column(s): {invalid}")
        if "aliases_json" in fields and isinstance(fields["aliases_json"], list):
            fields["aliases_json"] = json.dumps(fields["aliases_json"])
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE projects SET {set_clause} WHERE id = ?",
                [v for _, v in pairs] + [project_id],
            )
            await self._db.conn.commit()

    async def get_project(self, project_id: str) -> dict | None:
        cursor = await self._db.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def list_projects(
        self, client_id: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        conditions = []
        params: list = []
        if not include_archived:
            conditions.append("status = 'active'")
        if client_id:
            conditions.append("client_id = ?")
            params.append(client_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.conn.execute(
            f"SELECT * FROM projects {where} ORDER BY name COLLATE NOCASE", params
        )
        return [_row_to_dict(r) for r in await cursor.fetchall()]

    async def delete_project(self, project_id: str) -> bool:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "UPDATE meetings SET project_id = NULL, updated_at = ? WHERE project_id = ?",
                (time.time(), project_id),
            )
            cursor = await self._db.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            await self._db.conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Assignment helpers
    # ------------------------------------------------------------------

    async def roster(self) -> dict:
        """Active clients + projects in one shape for the assigner/UI."""
        return {
            "clients": await self.list_clients(),
            "projects": await self.list_projects(),
        }

    async def latest_assignment_for_series(self, series_id: str) -> dict | None:
        """Most recent assignment among a series' meetings (manual first)."""
        cursor = await self._db.conn.execute(
            """SELECT client_id, project_id, assignment_source
               FROM meetings
               WHERE series_id = ? AND (client_id IS NOT NULL OR project_id IS NOT NULL)
               ORDER BY (assignment_source = 'manual') DESC, started_at DESC
               LIMIT 1""",
            (series_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
