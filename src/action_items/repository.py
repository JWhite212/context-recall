"""Data access for action items."""

import logging
import time
import uuid
from datetime import date

from src.db.database import Database

logger = logging.getLogger("meetingmind.action_items")

_VALID_STATUSES = frozenset({"open", "in_progress", "done", "cancelled"})
_VALID_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})

# Columns that update() is allowed to write.
_MUTABLE_COLUMNS = frozenset(
    {
        "title",
        "description",
        "assignee",
        "status",
        "priority",
        "due_date",
        "reminder_at",
        "source",
        "extracted_text",
    }
)


class ActionItemRepository:
    """Async CRUD for the action_items table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        meeting_id: str,
        title: str,
        description: str | None = None,
        assignee: str | None = None,
        status: str = "open",
        priority: str = "medium",
        due_date: str | None = None,
        reminder_at: float | None = None,
        source: str = "manual",
        extracted_text: str | None = None,
    ) -> str:
        """Insert a new action item and return its ID."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: {priority!r}")

        item_id = str(uuid.uuid4())
        now = time.time()
        await self._db.conn.execute(
            """
            INSERT INTO action_items
                (id, meeting_id, title, description, assignee, status, priority,
                 due_date, reminder_at, source, extracted_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                meeting_id,
                title,
                description,
                assignee,
                status,
                priority,
                due_date,
                reminder_at,
                source,
                extracted_text,
                now,
                now,
            ),
        )
        await self._db.conn.commit()
        logger.debug("Created action item %s for meeting %s", item_id, meeting_id)
        return item_id

    async def get(self, item_id: str) -> dict | None:
        """Fetch a single action item by ID."""
        cursor = await self._db.conn.execute("SELECT * FROM action_items WHERE id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update(self, item_id: str, **fields) -> None:
        """Update one or more fields on an action item.

        Automatically manages ``completed_at``:
        - Setting status to ``"done"`` sets ``completed_at`` to now.
        - Setting status to ``"open"`` or ``"in_progress"`` clears ``completed_at``.
        """
        if not fields:
            return

        invalid = set(fields) - _MUTABLE_COLUMNS
        if invalid:
            raise ValueError(f"Cannot update column(s): {invalid}")

        if "status" in fields:
            status = fields["status"]
            if status not in _VALID_STATUSES:
                raise ValueError(f"Invalid status: {status!r}")
            if status == "done":
                fields["completed_at"] = time.time()
            elif status in ("open", "in_progress"):
                fields["completed_at"] = None

        if "priority" in fields and fields["priority"] not in _VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: {fields['priority']!r}")

        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        values = [v for _, v in pairs] + [item_id]

        await self._db.conn.execute(
            f"UPDATE action_items SET {set_clause} WHERE id = ?",
            values,
        )
        await self._db.conn.commit()

    async def delete(self, item_id: str) -> None:
        """Delete an action item by ID."""
        await self._db.conn.execute("DELETE FROM action_items WHERE id = ?", (item_id,))
        await self._db.conn.commit()

    async def list_items(
        self,
        status: str | None = None,
        assignee: str | None = None,
        due_before: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List action items with optional filters.

        Args:
            status: Filter by status (open, in_progress, done, cancelled).
            assignee: Filter by assignee name.
            due_before: Filter items due before this ISO date string.
            limit: Maximum number of results.
            offset: Number of results to skip.
        """
        conditions: list[str] = []
        params: list = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if assignee is not None:
            conditions.append("assignee = ?")
            params.append(assignee)
        if due_before is not None:
            conditions.append("due_date < ?")
            params.append(due_before)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        cursor = await self._db.conn.execute(
            f"SELECT * FROM action_items {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_by_meeting(self, meeting_id: str) -> list[dict]:
        """List all action items for a specific meeting."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM action_items WHERE meeting_id = ? ORDER BY created_at ASC",
            (meeting_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_overdue(self) -> list[dict]:
        """List action items that are overdue (open/in_progress with due_date in the past)."""
        today = date.today().isoformat()
        cursor = await self._db.conn.execute(
            "SELECT * FROM action_items "
            "WHERE status IN ('open', 'in_progress') AND due_date < ? "
            "ORDER BY due_date ASC",
            (today,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def list_due_reminders(self) -> list[dict]:
        """List action items with reminders that are due now or in the past."""
        now = time.time()
        cursor = await self._db.conn.execute(
            "SELECT * FROM action_items "
            "WHERE status IN ('open', 'in_progress') AND reminder_at <= ? "
            "ORDER BY reminder_at ASC",
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
