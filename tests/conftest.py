"""Shared test fixtures."""

import asyncio
from pathlib import Path

import pytest
import yaml

from src.api.events import EventBus
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import (
    AudioConfig,
    DetectionConfig,
    DiarisationConfig,
    MarkdownConfig,
    NotionConfig,
    SummarisationConfig,
)


class FakePlatform:
    """Controllable PlatformDetector for testing."""

    def __init__(self):
        self.app_running: bool = False
        self.audio_active: bool = False
        self.call_window_active: bool = False
        # Track which process names were passed.
        self.last_process_names: list[str] | None = None

    def is_app_running(self, process_names: list[str]) -> bool:
        self.last_process_names = process_names
        return self.app_running

    def is_app_using_audio(self, process_names: list[str]) -> bool:
        return self.audio_active

    def is_call_window_active(self) -> bool:
        return self.call_window_active


@pytest.fixture
def fake_platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def detection_config() -> DetectionConfig:
    """DetectionConfig with fast values for testing."""
    return DetectionConfig(
        poll_interval_seconds=0,
        min_meeting_duration_seconds=10,
        required_consecutive_detections=2,
        required_consecutive_end_detections=2,
    )


@pytest.fixture
def audio_config(tmp_path: Path) -> AudioConfig:
    return AudioConfig(temp_audio_dir=str(tmp_path))


@pytest.fixture
def summarisation_config() -> SummarisationConfig:
    return SummarisationConfig(
        backend="ollama",
        ollama_base_url="http://localhost:11434",
    )


@pytest.fixture
def diarisation_config() -> DiarisationConfig:
    return DiarisationConfig(enabled=True)


@pytest.fixture
def markdown_config(tmp_path: Path) -> MarkdownConfig:
    return MarkdownConfig(
        enabled=True,
        vault_path=str(tmp_path / "vault"),
        filename_template="{date}_{slug}.md",
        include_full_transcript=True,
    )


@pytest.fixture
def notion_config() -> NotionConfig:
    return NotionConfig(
        enabled=True,
        api_key="test-notion-key",
        database_id="test-db-id",
    )


@pytest.fixture
def sample_transcript() -> Transcript:
    """A Transcript with a few segments for testing."""
    return Transcript(
        segments=[
            TranscriptSegment(start=0.0, end=5.0, text="Hello everyone."),
            TranscriptSegment(start=5.0, end=12.0, text="Let's discuss the roadmap."),
            TranscriptSegment(
                start=12.0, end=20.0, text="We need to ship by Friday."
            ),
        ],
        language="en",
        language_probability=0.98,
        duration_seconds=20.0,
    )


@pytest.fixture
def sample_summary() -> MeetingSummary:
    """A MeetingSummary with test markdown content."""
    md = (
        "# Sprint Planning\n\n"
        "## Summary\nWe discussed the roadmap.\n\n"
        "## Key Decisions\n- Ship by Friday\n\n"
        "## Action Items\n- [ ] Finish tests\n\n"
        "## Open Questions\n- None\n\n"
        "## Tags\nplanning, roadmap\n"
    )
    return MeetingSummary(
        raw_markdown=md,
        title="Sprint Planning",
        tags=["planning", "roadmap"],
    )


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
