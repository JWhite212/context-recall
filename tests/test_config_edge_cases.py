"""Edge-case tests for src/utils/config.py — supplements test_config.py."""


import pytest
import yaml

from src.utils.config import DetectionConfig, _expand_path, load_config


def test_malformed_yaml_does_not_crash(tmp_path):
    """YAML that parses as a non-dict causes an AttributeError in load_config
    because raw.get() is called on a non-dict. Truly invalid YAML raises
    yaml.YAMLError. Both are acceptable failures."""
    # Truly unparseable YAML.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- :\n  - :\n    x: [")

    with pytest.raises((yaml.YAMLError, AttributeError)):
        load_config(config_path)


def test_detection_config_rejects_shell_injection():
    with pytest.raises(ValueError, match="Invalid process name"):
        DetectionConfig(process_names=["; rm -rf /"])


def test_detection_config_valid_process_names():
    """Valid process names should not raise."""
    config = DetectionConfig(
        process_names=["Microsoft Teams", "MSTeams", "Teams (work or school)"]
    )
    assert len(config.process_names) == 3
    assert "Microsoft Teams" in config.process_names
    assert "Teams (work or school)" in config.process_names


def test_env_var_expansion_in_path(monkeypatch):
    monkeypatch.setenv("MEETINGMIND_TEST_DIR", "/tmp/test-meeting-mind")
    result = _expand_path("$MEETINGMIND_TEST_DIR/output")
    assert "/tmp/test-meeting-mind/output" in result


def test_wrong_type_in_field():
    """Python dataclasses don't enforce types at runtime.

    Passing a string where int is expected should not raise during
    construction (Python dataclasses have no runtime type checking).
    """
    config = DetectionConfig(poll_interval_seconds="five")
    # The value is stored as-is — no type coercion or validation.
    assert config.poll_interval_seconds == "five"
