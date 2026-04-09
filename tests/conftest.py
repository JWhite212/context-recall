"""Shared test fixtures."""

import asyncio
from pathlib import Path

import pytest
import yaml

from src.api.events import EventBus
from src.db.database import Database
from src.db.repository import MeetingRepository


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Create a minimal config.yaml in a temp directory."""
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {"sample_rate": 16000},
        "transcription": {"model_size": "tiny.en"},
        "summarisation": {"backend": "ollama"},
        "api": {"host": "127.0.0.1", "port": 9876},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Provide a connected test database (cleaned up after test)."""
    database = Database(db_path=tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> MeetingRepository:
    """Provide a repository backed by the test database."""
    return MeetingRepository(db)


@pytest.fixture
def event_bus() -> EventBus:
    """Provide a fresh EventBus with an event loop set."""
    bus = EventBus()
    loop = asyncio.get_event_loop()
    bus.set_loop(loop)
    return bus
