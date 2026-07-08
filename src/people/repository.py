"""Data access for the people directory and voice-profile samples."""

import json
import logging
import struct
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.people")

_MUTABLE_COLUMNS = frozenset({"name", "email", "aliases_json", "notes", "is_me"})


class PersonRepository:
    """Async CRUD for people and their enrolled voice profiles."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # People
    # ------------------------------------------------------------------

    async def create(
        self,
        name: str,
        email: str = "",
        aliases: list[str] | None = None,
        notes: str = "",
        is_me: bool = False,
    ) -> str:
        person_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                """INSERT INTO people
                   (id, name, email, aliases_json, notes, is_me, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    person_id,
                    name,
                    email,
                    json.dumps(aliases or []),
                    notes,
                    1 if is_me else 0,
                    now,
                    now,
                ),
            )
            await self._db.conn.commit()
        logger.debug("Created person %s (%s)", person_id, name)
        return person_id

    async def update(self, person_id: str, **fields) -> None:
        if not fields:
            return
        invalid = set(fields) - _MUTABLE_COLUMNS
        if invalid:
            raise ValueError(f"Cannot update column(s): {invalid}")
        if "aliases_json" in fields and isinstance(fields["aliases_json"], list):
            fields["aliases_json"] = json.dumps(fields["aliases_json"])
        if "is_me" in fields:
            fields["is_me"] = 1 if fields["is_me"] else 0
        fields["updated_at"] = time.time()
        pairs = list(fields.items())
        set_clause = ", ".join(f"{k} = ?" for k, _ in pairs)
        values = [v for _, v in pairs] + [person_id]
        async with self._db.write_lock:
            await self._db.conn.execute(f"UPDATE people SET {set_clause} WHERE id = ?", values)
            await self._db.conn.commit()

    async def get(self, person_id: str) -> dict | None:
        cursor = await self._db.conn.execute(
            """SELECT p.*, COUNT(v.id) AS sample_count
               FROM people p LEFT JOIN voice_profiles v ON v.person_id = p.id
               WHERE p.id = ? GROUP BY p.id""",
            (person_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_people(self) -> list[dict]:
        cursor = await self._db.conn.execute(
            """SELECT p.*, COUNT(v.id) AS sample_count
               FROM people p LEFT JOIN voice_profiles v ON v.person_id = p.id
               GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def delete(self, person_id: str) -> bool:
        async with self._db.write_lock:
            cursor = await self._db.conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
            await self._db.conn.commit()
            return cursor.rowcount > 0

    async def find_by_name(self, name: str) -> dict | None:
        """Case-insensitive match on name or any alias."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM people WHERE name = ? COLLATE NOCASE", (name,)
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_dict(row)
        cursor = await self._db.conn.execute("SELECT * FROM people")
        for row in await cursor.fetchall():
            try:
                aliases = json.loads(row["aliases_json"] or "[]")
            except (ValueError, TypeError):
                aliases = []
            if any(a.lower() == name.lower() for a in aliases if isinstance(a, str)):
                return self._row_to_dict(row)
        return None

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["aliases"] = json.loads(d.pop("aliases_json", "[]") or "[]")
        except (ValueError, TypeError):
            d["aliases"] = []
        d["is_me"] = bool(d.get("is_me", 0))
        d.setdefault("sample_count", 0)
        return d

    # ------------------------------------------------------------------
    # Voice profiles
    # ------------------------------------------------------------------

    async def add_voice_sample(
        self,
        person_id: str,
        embedding: list[float],
        *,
        source_meeting_id: str | None = None,
        speaker_label: str = "",
        segment_count: int = 0,
        duration_seconds: float = 0.0,
        max_samples: int = 8,
    ) -> int:
        """Store an enrolment sample; prunes the oldest beyond *max_samples*."""
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        async with self._db.write_lock:
            cursor = await self._db.conn.execute(
                """INSERT INTO voice_profiles
                   (person_id, embedding, dim, source_meeting_id, speaker_label,
                    segment_count, duration_seconds, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    person_id,
                    blob,
                    len(embedding),
                    source_meeting_id,
                    speaker_label,
                    segment_count,
                    duration_seconds,
                    time.time(),
                ),
            )
            sample_id = cursor.lastrowid
            if max_samples > 0:
                await self._db.conn.execute(
                    """DELETE FROM voice_profiles WHERE person_id = ? AND id NOT IN (
                           SELECT id FROM voice_profiles WHERE person_id = ?
                           ORDER BY created_at DESC, id DESC LIMIT ?
                       )""",
                    (person_id, person_id, max_samples),
                )
            await self._db.conn.commit()
        return sample_id

    async def list_voice_samples(self, person_id: str) -> list[dict]:
        """Sample metadata (no embedding blobs) for the UI."""
        cursor = await self._db.conn.execute(
            """SELECT id, person_id, dim, source_meeting_id, speaker_label,
                      segment_count, duration_seconds, created_at
               FROM voice_profiles WHERE person_id = ? ORDER BY created_at DESC""",
            (person_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def delete_voice_sample(self, sample_id: int, person_id: str | None = None) -> bool:
        """Delete a sample; with *person_id* given, only if it belongs to them."""
        sql = "DELETE FROM voice_profiles WHERE id = ?"
        params: list = [sample_id]
        if person_id is not None:
            sql += " AND person_id = ?"
            params.append(person_id)
        async with self._db.write_lock:
            cursor = await self._db.conn.execute(sql, params)
            await self._db.conn.commit()
            return cursor.rowcount > 0

    async def get_all_voice_profiles(self) -> list[dict]:
        """Every enrolment sample joined with its person, for matching."""
        cursor = await self._db.conn.execute(
            """SELECT v.person_id, p.name, v.embedding
               FROM voice_profiles v JOIN people p ON p.id = v.person_id"""
        )
        rows = await cursor.fetchall()
        profiles = []
        for row in rows:
            blob = row["embedding"]
            num_floats = len(blob) // 4
            profiles.append(
                {
                    "person_id": row["person_id"],
                    "name": row["name"],
                    "embedding": list(struct.unpack(f"{num_floats}f", blob)),
                }
            )
        return profiles
