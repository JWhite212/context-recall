"""
Configuration loader for MeetingMind.

Reads config.yaml from the project root and exposes a typed
configuration object. Falls back to sensible defaults where possible.
"""

import dataclasses
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


# Resolve project root. Inside a PyInstaller frozen binary, __file__
# doesn't exist in the usual sense — use sys.executable instead.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


_SAFE_PROCESS_NAME = re.compile(r"^[\w\s.()\-]+$")


@dataclass
class DetectionConfig:
    poll_interval_seconds: int = 3
    min_meeting_duration_seconds: int = 30
    required_consecutive_detections: int = 3  # Debounce: consecutive positive polls needed.
    required_consecutive_end_detections: int = 2  # Debounce for meeting end.
    process_names: list[str] = field(
        default_factory=lambda: ["Microsoft Teams", "MSTeams", "Teams"]
    )

    def __post_init__(self) -> None:
        for name in self.process_names:
            if not _SAFE_PROCESS_NAME.match(name):
                raise ValueError(
                    f"Invalid process name {name!r}: only alphanumeric, "
                    f"spaces, dots, parens, and hyphens allowed"
                )


@dataclass
class AudioConfig:
    blackhole_device_name: str = "BlackHole 2ch"
    mic_device_name: str = ""  # Empty = system default input device.
    mic_enabled: bool = True   # Mix microphone input with system audio.
    mic_volume: float = 1.0    # Mic gain relative to system audio (0.0–2.0).
    system_volume: float = 1.0  # System audio gain after normalisation (0.0–2.0).
    sample_rate: int = 16000
    channels: int = 1
    temp_audio_dir: str = "~/Library/Caches/MeetingMind"
    keep_source_files: bool = False  # Keep separate source WAVs (for diarisation).


@dataclass
class TranscriptionConfig:
    model_size: str = "small.en"
    compute_type: str = "auto"
    language: str = "en"
    cpu_threads: int = 0
    vad_threshold: float = 0.35  # Silero VAD threshold (default 0.5; lower = less aggressive).


@dataclass
class SummarisationConfig:
    backend: str = "ollama"  # "claude" or "ollama"
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    def __repr__(self) -> str:
        key_display = "****" if self.anthropic_api_key else "<not set>"
        return (
            f"SummarisationConfig(backend={self.backend!r}, "
            f"anthropic_api_key={key_display!r}, "
            f"model={self.model!r}, max_tokens={self.max_tokens}, "
            f"ollama_base_url={self.ollama_base_url!r}, "
            f"ollama_model={self.ollama_model!r})"
        )


@dataclass
class MarkdownConfig:
    enabled: bool = True
    vault_path: str = "~/Documents/Meetings"
    filename_template: str = "{date}_{slug}.md"
    include_full_transcript: bool = True


@dataclass
class NotionConfig:
    enabled: bool = False
    api_key: str = ""
    database_id: str = ""
    properties: dict[str, str] = field(
        default_factory=lambda: {
            "title": "Name",
            "date": "Date",
            "tags": "Tags",
            "status": "Status",
        }
    )

    def __repr__(self) -> str:
        key_display = "****" if self.api_key else "<not set>"
        return (
            f"NotionConfig(enabled={self.enabled!r}, "
            f"api_key={key_display!r}, "
            f"database_id={self.database_id!r}, "
            f"properties={self.properties!r})"
        )


@dataclass
class DiarisationConfig:
    enabled: bool = False
    speaker_name: str = "Me"           # Label for the local user.
    remote_label: str = "Remote"       # Label for remote participants.
    energy_ratio_threshold: float = 1.5  # How much louder one source must be.


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "~/Library/Logs/meetingmind.log"


@dataclass
class ApiConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9876


@dataclass
class RetentionConfig:
    audio_retention_days: int = 0   # 0 = keep forever.
    record_retention_days: int = 0  # 0 = keep forever.


@dataclass
class AppConfig:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    summarisation: SummarisationConfig = field(default_factory=SummarisationConfig)
    diarisation: DiarisationConfig = field(default_factory=DiarisationConfig)
    markdown: MarkdownConfig = field(default_factory=MarkdownConfig)
    notion: NotionConfig = field(default_factory=NotionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)


def _expand_path(path_str: str) -> str:
    """Expand ~ and environment variables in a path string."""
    return str(Path(os.path.expandvars(os.path.expanduser(path_str))).resolve())


def _build_dataclass(cls, raw: dict):
    """
    Construct a dataclass from a dict, ignoring any keys that
    don't correspond to fields. This makes the config forward-compatible:
    old configs won't break if new fields are added.
    """
    valid_fields = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in raw.items() if k in valid_fields}
    return cls(**filtered)


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """
    Load and validate the application configuration.

    Falls back to defaults if the config file does not exist,
    which allows the app to start in a minimal state for testing.
    """
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        logger.warning("No config found at %s — using defaults.", path)
        return AppConfig()

    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    config = AppConfig(
        detection=_build_dataclass(DetectionConfig, raw.get("detection", {})),
        audio=_build_dataclass(AudioConfig, raw.get("audio", {})),
        transcription=_build_dataclass(
            TranscriptionConfig, raw.get("transcription", {})
        ),
        summarisation=_build_dataclass(
            SummarisationConfig, raw.get("summarisation", {})
        ),
        diarisation=_build_dataclass(
            DiarisationConfig, raw.get("diarisation", {})
        ),
        markdown=_build_dataclass(MarkdownConfig, raw.get("markdown", {})),
        notion=_build_dataclass(NotionConfig, raw.get("notion", {})),
        logging=_build_dataclass(LoggingConfig, raw.get("logging", {})),
        api=_build_dataclass(ApiConfig, raw.get("api", {})),
        retention=_build_dataclass(RetentionConfig, raw.get("retention", {})),
    )

    # Expand user paths so downstream code doesn't need to worry about tildes.
    config.audio.temp_audio_dir = _expand_path(config.audio.temp_audio_dir)
    config.markdown.vault_path = _expand_path(config.markdown.vault_path)
    config.logging.log_file = _expand_path(config.logging.log_file)

    return config
