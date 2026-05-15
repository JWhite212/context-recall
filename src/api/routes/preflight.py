"""
Pre-flight audio check endpoint.

GET /api/preflight — runs ``audio_preflight.run_preflight`` against the
current configuration and returns the report as JSON. Useful for the
Settings screen and for a one-shot pre-meeting sanity check from the UI.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from src.audio_preflight import run_preflight
from src.utils.config import AudioConfig, load_config

logger = logging.getLogger("contextrecall.api.preflight")

router = APIRouter()


@router.get("/api/preflight", summary="Pre-flight audio + permission checks")
async def preflight() -> dict[str, Any]:
    try:
        config = load_config()
        audio_config: AudioConfig = config.audio
    except Exception as e:
        logger.warning("Failed to load config for preflight: %s", e)
        # Fall back to defaults so the endpoint still returns useful
        # device-presence info instead of 500ing on a config error.
        audio_config = AudioConfig()

    report = run_preflight(audio_config)
    return report.to_dict()
