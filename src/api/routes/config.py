"""
Configuration read/write endpoints.

GET  /api/config  — returns current config.yaml as JSON (API keys masked).
PUT  /api/config  — merges partial updates into config.yaml.
"""

import copy
import dataclasses
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.utils.config import (
    ApiConfig,
    AppConfig,
    AudioConfig,
    DetectionConfig,
    DiarisationConfig,
    EmailChannelConfig,
    LoggingConfig,
    MarkdownConfig,
    NotificationsConfig,
    NotionConfig,
    RetentionConfig,
    SummarisationConfig,
    TranscriptionConfig,
    WebhookChannelConfig,
    _build_dataclass,
)

router = APIRouter()

_config_path: Path | None = None

# Fields that contain secrets — masked in GET, preserved on PUT if unchanged.
_SECRET_FIELDS = {
    ("summarisation", "anthropic_api_key"),
    ("notion", "api_key"),
    ("notifications", "email", "smtp_password"),
}

_MASK = "••••••••"


def init(config_path: Path) -> None:
    global _config_path
    _config_path = config_path


def _read_yaml() -> dict:
    if not _config_path or not _config_path.exists():
        return {}
    with open(_config_path, "r") as f:
        return yaml.safe_load(f) or {}


def _full_config_dict(raw: dict) -> dict:
    """Build a complete config dict with dataclass defaults for any missing fields."""
    config = AppConfig(
        detection=_build_dataclass(DetectionConfig, raw.get("detection", {})),
        audio=_build_dataclass(AudioConfig, raw.get("audio", {})),
        transcription=_build_dataclass(TranscriptionConfig, raw.get("transcription", {})),
        summarisation=_build_dataclass(SummarisationConfig, raw.get("summarisation", {})),
        diarisation=_build_dataclass(DiarisationConfig, raw.get("diarisation", {})),
        markdown=_build_dataclass(MarkdownConfig, raw.get("markdown", {})),
        notion=_build_dataclass(NotionConfig, raw.get("notion", {})),
        logging=_build_dataclass(LoggingConfig, raw.get("logging", {})),
        api=_build_dataclass(ApiConfig, raw.get("api", {})),
        retention=_build_dataclass(RetentionConfig, raw.get("retention", {})),
        notifications=_build_dataclass(NotificationsConfig, raw.get("notifications", {})),
    )
    # Handle nested notification channel configs.
    notif_raw = raw.get("notifications", {})
    if "webhook" in notif_raw and isinstance(notif_raw["webhook"], dict):
        config.notifications.webhook = _build_dataclass(WebhookChannelConfig, notif_raw["webhook"])
    if "email" in notif_raw and isinstance(notif_raw["email"], dict):
        config.notifications.email = _build_dataclass(EmailChannelConfig, notif_raw["email"])
    return dataclasses.asdict(config)


def _mask_secrets(config: dict) -> dict:
    """Replace secret values with a mask, preserving structure."""
    masked = copy.deepcopy(config)
    for path in _SECRET_FIELDS:
        node = masked
        for segment in path[:-1]:
            node = node.get(segment, {})
            if not isinstance(node, dict):
                break
        else:
            key = path[-1]
            if key in node:
                val = node[key]
                if isinstance(val, str) and val.strip():
                    node[key] = _MASK
    return masked


def _deep_merge(base: dict, updates: dict, existing: dict, _path: tuple[str, ...] = ()) -> dict:
    """
    Recursively merge *updates* into *base*.

    If an update value for a secret field equals the mask, the original
    value from *existing* is kept (the user didn't change it).
    """
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value, existing.get(key, {}), _path + (key,))
        else:
            full_path = _path + (key,)
            if full_path in _SECRET_FIELDS and value == _MASK:
                value = existing.get(key, "")
            merged[key] = value
    return merged


@router.get("/api/config", summary="Get current configuration")
async def get_config():
    raw = _read_yaml()
    full = _full_config_dict(raw)
    return _mask_secrets(full)


class ConfigUpdateBody(BaseModel):
    """Validated schema for config updates — rejects unknown top-level keys."""

    model_config = ConfigDict(extra="forbid")

    detection: dict | None = None
    audio: dict | None = None
    transcription: dict | None = None
    summarisation: dict | None = None
    diarisation: dict | None = None
    markdown: dict | None = None
    notion: dict | None = None
    logging: dict | None = None
    api: dict | None = None
    calendar: dict | None = None
    retention: dict | None = None
    notifications: dict | None = None


@router.put("/api/config", summary="Update configuration")
async def update_config(body: ConfigUpdateBody):
    if not _config_path:
        raise HTTPException(status_code=500, detail="Config path not set")

    existing = _read_yaml()
    merged = _deep_merge(existing, body.model_dump(exclude_none=True), existing)

    with open(_config_path, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return _mask_secrets(_full_config_dict(merged))
