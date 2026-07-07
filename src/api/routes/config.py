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
from pydantic import ConfigDict, create_model

from src.utils.config import (
    AppConfig,
    EmailChannelConfig,
    NotificationsConfig,
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
    """Build a complete config dict with dataclass defaults for any missing fields.

    Sections are derived from AppConfig's own fields so a new config section
    is automatically read from (and written back to) the YAML without this
    module needing to know about it.
    """
    sections = {}
    for f in dataclasses.fields(AppConfig):
        section_raw = raw.get(f.name, {})
        if not isinstance(section_raw, dict):
            section_raw = {}
        section_cls = f.default_factory
        if f.name == "notifications":
            # Strip nested channel dicts so _build_dataclass doesn't pass
            # them as scalars, then rebuild them explicitly.
            notif_base = {k: v for k, v in section_raw.items() if k not in {"webhook", "email"}}
            notifications = _build_dataclass(NotificationsConfig, notif_base)
            webhook_raw = section_raw.get("webhook", {})
            if isinstance(webhook_raw, dict):
                notifications.webhook = _build_dataclass(WebhookChannelConfig, webhook_raw)
            email_raw = section_raw.get("email", {})
            if isinstance(email_raw, dict):
                notifications.email = _build_dataclass(EmailChannelConfig, email_raw)
            sections[f.name] = notifications
        else:
            sections[f.name] = _build_dataclass(section_cls, section_raw)
    return dataclasses.asdict(AppConfig(**sections))


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


# Validated schema for config updates — accepts exactly the sections
# AppConfig defines (generated, so it cannot drift when sections are added)
# and rejects unknown top-level keys. A hand-maintained copy of this list
# once missed action_items/series/analytics/prep, which broke every save
# from the UI with "Extra inputs are not permitted".
ConfigUpdateBody = create_model(
    "ConfigUpdateBody",
    __config__=ConfigDict(extra="forbid"),
    **{f.name: (dict | None, None) for f in dataclasses.fields(AppConfig)},
)


@router.put("/api/config", summary="Update configuration")
async def update_config(body: ConfigUpdateBody):
    if not _config_path:
        raise HTTPException(status_code=500, detail="Config path not set")

    existing = _read_yaml()
    merged = _deep_merge(existing, body.model_dump(exclude_none=True), existing)

    with open(_config_path, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return _mask_secrets(_full_config_dict(merged))
