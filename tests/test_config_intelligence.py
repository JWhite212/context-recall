import yaml

from src.utils.config import (
    ActionItemsConfig,  # noqa: F401
    AnalyticsConfig,  # noqa: F401
    AppConfig,
    NotificationsConfig,  # noqa: F401
    PrepConfig,  # noqa: F401
    SeriesConfig,  # noqa: F401
    load_config,
)


def test_new_config_sections_have_defaults():
    config = AppConfig()
    assert config.action_items.auto_extract is True
    assert config.series.min_meetings_for_series == 3
    assert config.analytics.refresh_interval_hours == 6
    assert config.notifications.enabled is True
    assert config.prep.lead_time_minutes == 15


def test_new_config_sections_load_from_yaml(tmp_path):
    config_data = {
        "action_items": {"auto_extract": False, "duplicate_threshold": 0.9},
        "series": {"heuristic_enabled": False},
        "analytics": {"health_alert_threshold": 2.0},
        "notifications": {"enabled": False},
        "prep": {"lead_time_minutes": 30, "max_context_meetings": 5},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config_data))
    config = load_config(path)
    assert config.action_items.auto_extract is False
    assert config.action_items.duplicate_threshold == 0.9
    assert config.series.heuristic_enabled is False
    assert config.analytics.health_alert_threshold == 2.0
    assert config.notifications.enabled is False
    assert config.prep.lead_time_minutes == 30
    assert config.prep.max_context_meetings == 5
