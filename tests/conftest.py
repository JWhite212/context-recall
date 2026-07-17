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


@pytest.fixture(autouse=True)
def _no_real_coreaudio(monkeypatch):
    """The suite must never mutate the host's audio configuration.

    Any code path that constructs a real CoreAudioBackend sees it as
    unavailable, so AudioRouter degrades to a graceful no-op. Routing
    logic is tested against FakeBackend in test_audio_routing.py.
    """
    monkeypatch.setattr("src.audio_routing.CoreAudioBackend.available", lambda self: False)


@pytest.fixture(autouse=True)
def _no_real_tcc_prompt(monkeypatch):
    """The suite must never trigger a macOS microphone-permission dialog.

    Every test sees the microphone as already authorized; a stray call
    to request_access() fails loudly. Tests that exercise the permission
    logic override these per-test (their monkeypatch wins because it is
    applied after this autouse fixture).
    """
    monkeypatch.setattr("src.mic_permission.authorization_status", lambda: "authorized")

    def _forbidden(**_kwargs):
        raise AssertionError("request_access() must never run inside the test suite")

    monkeypatch.setattr("src.mic_permission.request_access", _forbidden)
    monkeypatch.setattr("src.mic_permission.trigger_prompt_via_input_probe", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _no_real_calendar_tcc(monkeypatch):
    """The suite must never trigger a macOS calendar-permission dialog.

    With ``calendar.enabled`` defaulting True, every ``ContextRecall(...)``
    construction builds a CalendarMatcher — and EventKit IS importable in a
    dev venv, so without this guard that fires a real (blocking, up to 60s)
    TCC request. EventKit is made invisible to both the matcher and the
    reader (the reader imports ``_is_eventkit_available`` into its OWN
    namespace, so it must be patched separately), status introspection
    reports a benign non-authorized value, and a stray ``request_access()``
    fails loudly. Tests that exercise these paths override per-test (their
    monkeypatch wins because it is applied after this autouse fixture).
    """
    monkeypatch.setattr("src.calendar_matcher._is_eventkit_available", lambda: False)
    monkeypatch.setattr("src.calendar_events.reader._is_eventkit_available", lambda: False)
    monkeypatch.setattr("src.calendar_permission.authorization_status", lambda: "not_determined")

    def _forbidden(**_kwargs):
        raise AssertionError("calendar request_access() must never run inside the test suite")

    monkeypatch.setattr("src.calendar_permission.request_access", _forbidden)


@pytest.fixture(autouse=True)
def _reset_calendar_shared_store():
    """B1: the process-wide EKEventStore is a module singleton. Reset it
    around every test so a fake store built from one test's injected EventKit
    module never leaks into the next."""
    from src import calendar_permission

    calendar_permission.reset_shared_store()
    yield
    calendar_permission.reset_shared_store()


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
        min_gap_before_new_meeting=0,
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
            TranscriptSegment(start=12.0, end=20.0, text="We need to ship by Friday."),
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
