"""
SQLite database manager for MeetingMind.

Handles schema creation, migrations, and provides an async connection
interface via aiosqlite. The database stores meeting history, transcripts,
and summaries for the UI to query.

Location: ~/.local/share/meetingmind/meetings.db
"""

import logging
import os
from pathlib import Path

import aiosqlite

logger = logging.getLogger("meetingmind.db")

# XDG-style data directory (works on macOS and Linux).
DEFAULT_DB_DIR = Path(os.path.expanduser("~/.local/share/meetingmind"))
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "meetings.db"

SCHEMA_VERSION = 6

_vec_available = False


def _load_vec_extension(conn):
    """Load sqlite-vec extension. Returns True if successful."""
    global _vec_available
    try:
        import sqlite_vec

        sqlite_vec.load(conn)
        _vec_available = True
        logger.info("sqlite-vec extension loaded successfully")
        return True
    except (ImportError, Exception) as e:
        logger.warning(
            "sqlite-vec not available; vector search will use brute-force fallback: %s",
            e,
        )
        _vec_available = False
        return False


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'recording',
    audio_path TEXT,
    transcript_json TEXT,
    summary_markdown TEXT,
    tags TEXT,
    language TEXT,
    word_count INTEGER,
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_started_at ON meetings(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
    title,
    summary_markdown,
    transcript_text,
    content='meetings',
    content_rowid='rowid'
);
"""

EMBEDDINGS_SQL = """
CREATE TABLE IF NOT EXISTS segment_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT DEFAULT '',
    start_time REAL NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_segment_embeddings_meeting
    ON segment_embeddings(meeting_id);
"""

VEC_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS segment_embeddings_vec USING vec0(
    embedding float[384]
);
"""

SPEAKER_MAPPINGS_SQL = """
CREATE TABLE IF NOT EXISTS speaker_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT DEFAULT 'manual',
    created_at REAL NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    UNIQUE(meeting_id, speaker_id)
);

CREATE INDEX IF NOT EXISTS idx_speaker_mappings_meeting
    ON speaker_mappings(meeting_id);
"""


_ALLOWED_TABLES = frozenset({"meetings", "speaker_mappings", "segment_embeddings"})
_ALLOWED_COL_TYPES = frozenset({"TEXT", "REAL", "INTEGER", "BLOB"})


async def _safe_add_column(conn, table: str, column: str, col_type: str, default: str) -> None:
    """Add a column if it doesn't already exist. Validates inputs to prevent SQL injection."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table!r}")
    if not column.isidentifier():
        raise ValueError(f"Invalid column name: {column!r}")
    if col_type not in _ALLOWED_COL_TYPES:
        raise ValueError(f"Invalid column type: {col_type!r}")
    try:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._connection: aiosqlite.Connection | None = None

    @property
    def path(self) -> Path:
        return self._db_path

    async def connect(self) -> None:
        """Open the database and ensure schema is up to date."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self._db_path))
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        # Load sqlite-vec extension (must happen before migration to create vec0 tables).
        # aiosqlite wraps a real sqlite3 connection — access it for extension loading.
        raw_conn = self._connection._conn  # Access underlying sqlite3.Connection
        _load_vec_extension(raw_conn)
        await self._migrate()
        logger.info("Database connected: %s", self._db_path)

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._connection

    async def _migrate(self) -> None:
        """Apply schema migrations based on user_version pragma."""
        cursor = await self.conn.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version < 1:
            await self.conn.executescript(SCHEMA_SQL)
            # FTS requires separate execution (can't be in executescript with IF NOT EXISTS
            # for virtual tables on some SQLite versions).
            try:
                await self.conn.executescript(FTS_SQL)
            except Exception:
                logger.warning("FTS5 not available; full-text search disabled.")
            await self.conn.executescript(EMBEDDINGS_SQL)
            if _vec_available:
                try:
                    await self.conn.executescript(VEC_SQL)
                except Exception:
                    logger.warning("Failed to create vec0 table on fresh install")
            await self.conn.executescript(SPEAKER_MAPPINGS_SQL)
            # Calendar integration columns (v6).
            await _safe_add_column(self.conn, "meetings", "calendar_event_title", "TEXT", "''")
            await _safe_add_column(self.conn, "meetings", "attendees_json", "TEXT", "'[]'")
            await _safe_add_column(self.conn, "meetings", "calendar_confidence", "REAL", "0.0")
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database schema created (version %d)", SCHEMA_VERSION)
        elif current_version < 2:
            await self.conn.execute(
                "ALTER TABLE meetings ADD COLUMN label TEXT NOT NULL DEFAULT ''"
            )
            await self.conn.executescript(EMBEDDINGS_SQL)
            await self.conn.executescript(SPEAKER_MAPPINGS_SQL)
            await self.conn.commit()
            current_version = 4
            logger.info("Database migrated to version 4")
        if current_version == 2:
            await self.conn.executescript(EMBEDDINGS_SQL)
            await self.conn.executescript(SPEAKER_MAPPINGS_SQL)
            await self.conn.commit()
            current_version = 4
            logger.info("Database migrated to version 4")
        if current_version == 3:
            await self.conn.executescript(SPEAKER_MAPPINGS_SQL)
            await self.conn.commit()
            current_version = 4
            logger.info("Database migrated to version 4")
        if current_version == 4:
            # sqlite-vec virtual table for vector search.
            if _vec_available:
                try:
                    await self.conn.executescript(VEC_SQL)
                    # Migrate existing embeddings into vec0 table.
                    cursor = await self.conn.execute("SELECT id, embedding FROM segment_embeddings")
                    rows = await cursor.fetchall()
                    count = 0
                    for row in rows:
                        await self.conn.execute(
                            "INSERT INTO segment_embeddings_vec(rowid, embedding) VALUES (?, ?)",
                            (row["id"], row["embedding"]),
                        )
                        count += 1
                    logger.info("Migrated %d embeddings to vec0 table", count)
                except Exception as e:
                    logger.warning("Failed to create vec0 table: %s", e)
            await self.conn.execute("PRAGMA user_version = 5")
            await self.conn.commit()
            logger.info("Database migrated to version 5 (sqlite-vec)")
            current_version = 5
        if current_version < 6:
            # Calendar integration columns.
            await _safe_add_column(self.conn, "meetings", "calendar_event_title", "TEXT", "''")
            await _safe_add_column(self.conn, "meetings", "attendees_json", "TEXT", "'[]'")
            await _safe_add_column(self.conn, "meetings", "calendar_confidence", "REAL", "0.0")
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version %d (calendar columns)", SCHEMA_VERSION)
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
