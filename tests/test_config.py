"""Tests for config loading and dataclass construction."""

import os
import sys
from pathlib import Path

import yaml

from src.utils.config import (
    AppConfig,
    AudioConfig,
    DetectionConfig,
    TranscriptionConfig,
    _build_dataclass,
    _default_config_path,
    _expand_path,
    load_config,
)


def test_load_valid_config(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "detection": {"poll_interval_seconds": 5},
                "audio": {"mic_enabled": False},
                "transcription": {"model_size": "medium.en"},
            }
        )
    )
    config = load_config(config_file)
    assert config.detection.poll_interval_seconds == 5
    assert config.audio.mic_enabled is False
    assert config.transcription.model_size == "medium.en"


def test_load_missing_file_returns_defaults():
    config = load_config(Path("/nonexistent/config.yaml"))
    assert isinstance(config, AppConfig)
    assert config.detection.poll_interval_seconds == 3
    assert config.audio.mic_enabled is True
    assert config.transcription.model_size == "mlx-community/whisper-large-v3-turbo"


def test_unknown_keys_ignored():
    """_build_dataclass should silently skip keys not in the dataclass."""
    result = _build_dataclass(
        DetectionConfig,
        {
            "poll_interval_seconds": 10,
            "some_future_key": "value",
            "another_unknown": 42,
        },
    )
    assert result.poll_interval_seconds == 10
    assert not hasattr(result, "some_future_key")


def test_build_dataclass_tolerates_none_section():
    """An empty YAML section (e.g. ``insights:`` with commented-out keys)
    parses as ``None`` — build the dataclass with defaults rather than crash."""
    result = _build_dataclass(DetectionConfig, None)
    assert result == DetectionConfig()


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


def test_missing_file_still_expands_user_paths():
    """When the config file is missing, load_config early-returns AppConfig()
    with dataclass defaults. Those defaults contain literal '~' (e.g.
    log_file='~/Library/Logs/Context Recall/contextrecall.log') and the
    happy-path _expand_path() calls never run.

    Symptom in production (2026-05-15 install): the installed daemon's
    cwd was the .app bundle, so logging.FileHandler resolved
    '~/Library/...' relative to that cwd and wrote app logs to a literal
    '~' directory INSIDE the .app bundle — invisible to the user and
    deleted on every reinstall.

    Expansion of '~' in path defaults must happen regardless of whether
    a config file was loaded."""
    config = load_config(Path("/definitely/does/not/exist.yaml"))

    assert "~" not in config.logging.log_file, (
        f"log_file must have ~ expanded even when no config file is loaded; "
        f"got {config.logging.log_file!r}"
    )
    assert "~" not in config.audio.temp_audio_dir, (
        f"temp_audio_dir must have ~ expanded; got {config.audio.temp_audio_dir!r}"
    )
    assert "~" not in config.markdown.vault_path, (
        f"vault_path must have ~ expanded; got {config.markdown.vault_path!r}"
    )
    assert config.logging.log_file.startswith(os.path.expanduser("~")), (
        f"log_file should resolve into the user's home directory; got {config.logging.log_file!r}"
    )


def test_default_config_path_frozen_uses_app_support_dir(monkeypatch):
    """For a frozen (PyInstaller) build, the daemon binary lives inside
    the .app bundle at /Applications/<App>.app/Contents/Resources/.../
    context-recall-daemon/. Resolving DEFAULT_CONFIG_PATH relative to
    sys.executable.parent puts it INSIDE the bundle, where no user-
    editable config can exist — that's why the installed daemon ran on
    pure defaults for the whole 2026-05-15 install.

    Frozen builds must look in the user's Application Support dir so
    customisations actually take effect."""
    from src.utils import paths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    resolved = _default_config_path()

    expected = paths.app_support_dir() / "config.yaml"
    assert resolved == expected, f"frozen-build config path must be {expected}, got {resolved}"
    # And it must not live inside the .app bundle.
    assert "/Applications/" not in str(resolved), (
        f"frozen config path must not point into the .app bundle; got {resolved}"
    )


def test_default_config_path_source_uses_project_root(monkeypatch):
    """Counterpart: source runs (pytest, `python -m src.main` from a
    checkout) must keep resolving next to the project, not Application
    Support, so dev workflows are unchanged."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    resolved = _default_config_path()

    assert resolved.name == "config.yaml"
    assert "Application Support" not in str(resolved), (
        f"source-run config path must not point at Application Support; got {resolved}"
    )


# ---------------------------------------------------------------------------
# Default-config materialisation + warn-once (installed daemon had no
# config.yaml and logged "No config found" once per minute forever).
# ---------------------------------------------------------------------------


def test_materialise_default_config_creates_file(tmp_path: Path):
    from src.utils.config import load_config, materialise_default_config

    target = tmp_path / "config.yaml"
    assert materialise_default_config(target) is True
    assert target.exists()

    # The written file must load back cleanly with default values.
    config = load_config(target)
    assert config.detection.poll_interval_seconds == 3
    assert config.audio.blackhole_device_name == "BlackHole 2ch"
    assert config.api.port == 9876


def test_materialise_default_config_never_overwrites(tmp_path: Path):
    from src.utils.config import materialise_default_config

    target = tmp_path / "config.yaml"
    target.write_text("detection:\n  poll_interval_seconds: 42\n")
    assert materialise_default_config(target) is False
    assert "42" in target.read_text()


def test_materialise_default_config_survives_unwritable_dir(tmp_path: Path):
    from src.utils.config import materialise_default_config

    target = tmp_path / "nope" / "config.yaml"
    tmp_path.chmod(0o500)
    try:
        assert materialise_default_config(target) is False
    finally:
        tmp_path.chmod(0o700)


def test_missing_config_warns_only_once_per_path(tmp_path: Path, caplog):
    import logging as _logging

    from src.utils.config import load_config

    missing = tmp_path / "does-not-exist.yaml"
    with caplog.at_level(_logging.WARNING, logger="src.utils.config"):
        load_config(missing)
        load_config(missing)
        load_config(missing)

    warnings = [r for r in caplog.records if "No config found" in r.getMessage()]
    assert len(warnings) == 1


def test_summarisation_config_has_template_auto_select_defaults():
    from src.utils.config import SummarisationConfig

    cfg = SummarisationConfig()
    assert cfg.auto_select_template is True
    assert cfg.template_select_min_confidence == 0.6


def test_insights_config_defaults():
    from src.utils.config import InsightsConfig

    cfg = InsightsConfig()
    assert cfg.enabled is True
    assert cfg.auto_extract is True


def test_automations_config_defaults():
    from src.utils.config import AutomationsConfig

    cfg = AutomationsConfig()
    assert cfg.enabled is True


def test_calendar_config_import_defaults():
    from src.utils.config import CalendarConfig

    cfg = CalendarConfig()
    assert cfg.import_enabled is True
    assert cfg.sync_interval_minutes == 15
    assert cfg.sync_horizon_days == 21
    assert cfg.excluded_calendars == []


def test_calendar_enabled_defaults_true():
    from src.utils.config import CalendarConfig

    assert CalendarConfig().enabled is True


def test_prep_config_sweep_defaults():
    from src.utils.config import PrepConfig

    cfg = PrepConfig()
    assert cfg.auto_generate is True
    assert cfg.lookahead_hours == 24
    assert cfg.sweep_interval_minutes == 15
    assert cfg.max_per_sweep == 5


def test_auto_arm_config_defaults(tmp_path):
    """auto_arm section defaults to off with the documented values."""
    from src.utils.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("detection:\n  poll_interval_seconds: 3\n")
    cfg = load_config(path)

    assert cfg.auto_arm.enabled is False
    assert cfg.auto_arm.lead_minutes == 2
    assert cfg.auto_arm.trailing_minutes == 5
    assert cfg.auto_arm.activity_rms_dbfs == -45.0
    assert cfg.auto_arm.activity_sustain_seconds == 3
    assert cfg.auto_arm.meeting_process_names == [
        "zoom.us",
        "Microsoft Teams",
        "Google Chrome",
    ]


def test_auto_arm_config_overrides_from_yaml(tmp_path):
    """Values in config.yaml override the dataclass defaults."""
    from src.utils.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "auto_arm:\n  enabled: true\n  lead_minutes: 5\n  meeting_process_names:\n    - zoom.us\n"
    )
    cfg = load_config(path)

    assert cfg.auto_arm.enabled is True
    assert cfg.auto_arm.lead_minutes == 5
    assert cfg.auto_arm.meeting_process_names == ["zoom.us"]


def test_diarisation_defaults_to_pyannote_enabled():
    from src.utils.config import DiarisationConfig

    cfg = DiarisationConfig()
    assert cfg.enabled is True
    assert cfg.backend == "pyannote"
