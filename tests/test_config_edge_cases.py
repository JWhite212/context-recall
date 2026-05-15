"""Edge-case tests for src/utils/config.py — supplements test_config.py."""

import pytest
import yaml

from src.utils.config import AudioConfig, DetectionConfig, _expand_path, load_config


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
    config = DetectionConfig(process_names=["Microsoft Teams", "MSTeams", "Teams (work or school)"])
    assert len(config.process_names) == 3
    assert "Microsoft Teams" in config.process_names
    assert "Teams (work or school)" in config.process_names


def test_env_var_expansion_in_path(monkeypatch):
    monkeypatch.setenv("CONTEXTRECALL_TEST_DIR", "/tmp/test-context-recall")
    result = _expand_path("$CONTEXTRECALL_TEST_DIR/output")
    assert "/tmp/test-context-recall/output" in result


def test_wrong_type_in_field():
    """Python dataclasses don't enforce types at runtime.

    Passing a string where int is expected should not raise during
    construction (Python dataclasses have no runtime type checking).
    """
    config = DetectionConfig(poll_interval_seconds="five")
    # The value is stored as-is — no type coercion or validation.
    assert config.poll_interval_seconds == "five"


def test_empty_process_names_list(tmp_path):
    """An empty process_names list is valid — no iteration needed."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"detection": {"process_names": []}}))
    config = load_config(config_path)
    assert config.detection.process_names == []


def test_expand_path_nonexistent_env_var():
    """os.path.expandvars leaves undefined variables as literal strings."""
    result = _expand_path("$NONEXISTENT_VAR_12345/foo")
    assert "$NONEXISTENT_VAR_12345" in result


def test_yaml_list_instead_of_dict(tmp_path):
    """A YAML file containing a list (not a dict) should fail gracefully.

    load_config calls raw.get(...) on the parsed YAML.  When the top-level
    value is a list, .get() does not exist, so an AttributeError is raised.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- item1\n- item2\n")

    with pytest.raises(AttributeError):
        load_config(config_path)


@pytest.mark.parametrize("bad_value", [0.0, 1e-9, 1e-8, 1e-1, 1.0, -1e-5])
def test_audio_config_rejects_silence_alert_threshold_out_of_range(bad_value):
    """silence_alert_threshold must sit inside 1e-7..1e-2 so the detector
    does not either suppress legitimate audio or chase interface dither."""
    with pytest.raises(ValueError, match="silence_alert_threshold"):
        AudioConfig(silence_alert_threshold=bad_value)


@pytest.mark.parametrize("good_value", [1e-7, 1e-5, 1e-4, 1e-3, 1e-2])
def test_audio_config_accepts_silence_alert_threshold_in_range(good_value):
    """Values inside the supported band must construct cleanly."""
    config = AudioConfig(silence_alert_threshold=good_value)
    assert config.silence_alert_threshold == good_value
