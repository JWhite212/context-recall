"""Tests for config loading and dataclass construction."""

import os
from pathlib import Path

import yaml

from src.utils.config import (
    AppConfig,
    AudioConfig,
    DetectionConfig,
    TranscriptionConfig,
    _build_dataclass,
    _expand_path,
    load_config,
)


def test_load_valid_config(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "detection": {"poll_interval_seconds": 5},
        "audio": {"mic_enabled": False},
        "transcription": {"model_size": "medium.en"},
    }))
    config = load_config(config_file)
    assert config.detection.poll_interval_seconds == 5
    assert config.audio.mic_enabled is False
    assert config.transcription.model_size == "medium.en"


def test_load_missing_file_returns_defaults():
    config = load_config(Path("/nonexistent/config.yaml"))
    assert isinstance(config, AppConfig)
    assert config.detection.poll_interval_seconds == 3
    assert config.audio.mic_enabled is True
    assert config.transcription.model_size == "small.en"


def test_unknown_keys_ignored():
    """_build_dataclass should silently skip keys not in the dataclass."""
    result = _build_dataclass(DetectionConfig, {
        "poll_interval_seconds": 10,
        "some_future_key": "value",
        "another_unknown": 42,
    })
    assert result.poll_interval_seconds == 10
    assert not hasattr(result, "some_future_key")


def test_expand_path_tilde():
    expanded = _expand_path("~/Documents/test")
    assert "~" not in expanded
    assert expanded.startswith(os.path.expanduser("~"))


def test_expand_path_absolute():
    expanded = _expand_path("/absolute/path")
    assert expanded == "/absolute/path"


def test_dataclass_defaults_valid_types():
    """All default config values should be their expected types."""
    config = AppConfig()
    assert isinstance(config.detection, DetectionConfig)
    assert isinstance(config.audio, AudioConfig)
    assert isinstance(config.transcription, TranscriptionConfig)
    assert isinstance(config.detection.poll_interval_seconds, int)
    assert isinstance(config.audio.sample_rate, int)
    assert isinstance(config.audio.mic_enabled, bool)


def test_load_empty_yaml(tmp_path: Path):
    """An empty YAML file should produce default config."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")
    config = load_config(config_file)
    assert isinstance(config, AppConfig)
    assert config.detection.poll_interval_seconds == 3
