"""
Diagnostics endpoint.

GET /api/diagnostics returns a summary of environment checks the UI
can use to diagnose first-run problems without reading source code.

Read-only and side-effect free; safe to call repeatedly.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
from typing import Any

import httpx
import sounddevice as sd
from fastapi import APIRouter

from src.utils import paths
from src.utils.config import load_config

logger = logging.getLogger("contextrecall.api.diagnostics")

router = APIRouter()


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _blackhole_present() -> bool:
    if not _is_macos():
        return False
    try:
        for device in sd.query_devices():  # type: ignore[attr-defined]
            name = str(device.get("name", "")).lower()
            if "blackhole" in name:
                return True
    except Exception:
        return False
    return False


def _blackhole_input_candidates() -> list[str]:
    """Return names of input devices matching 'blackhole' (case-insensitive).

    The audio capture path uses substring matching on input devices to
    pick the BlackHole interface, so this is exactly the set of valid
    values for AudioConfig.blackhole_device_name (Bug A3). Output-only
    entries with 'blackhole' in their name are excluded — they can't be
    used as a capture source.
    """
    try:
        return [
            str(d.get("name", ""))
            for d in sd.query_devices()  # type: ignore[attr-defined]
            if d.get("max_input_channels", 0) > 0 and "blackhole" in str(d.get("name", "")).lower()
        ]
    except Exception:
        return []


def _configured_blackhole_available(configured_name: str, candidates: list[str]) -> bool:
    """Mirror audio_capture._find_device's substring match: configured_name
    matches if it appears (case-insensitive) within any candidate name."""
    if not configured_name:
        return False
    needle = configured_name.lower()
    return any(needle in name.lower() for name in candidates)


def _audio_output_devices() -> list[str]:
    try:
        names: list[str] = []
        for device in sd.query_devices():  # type: ignore[attr-defined]
            if device.get("max_output_channels", 0) > 0:
                names.append(str(device.get("name", "")))
        return names
    except Exception:
        return []


def _microphone_available() -> bool:
    try:
        for device in sd.query_devices():  # type: ignore[attr-defined]
            if device.get("max_input_channels", 0) > 0:
                return True
    except Exception:
        return False
    return False


async def _ollama_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            res = await client.get("http://127.0.0.1:11434/api/tags")
            return res.status_code == 200
    except Exception:
        return False


async def _selected_ollama_model_available(model_name: str) -> bool:
    """Check whether the configured Ollama model is present locally.

    Matches by the prefix before any ``:tag`` suffix so ``qwen3`` matches
    ``qwen3:30b-a3b`` and vice versa.
    """
    if not model_name:
        return False
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            res = await client.get("http://127.0.0.1:11434/api/tags")
            if res.status_code != 200:
                return False
            data = res.json()
    except Exception:
        return False

    target_prefix = model_name.split(":", 1)[0]
    for entry in data.get("models") or []:
        name = str(entry.get("name", ""))
        if name.split(":", 1)[0] == target_prefix:
            return True
    return False


def _mlx_available() -> bool:
    try:
        import mlx.core  # noqa: F401

        return True
    except Exception:
        return False


def _whisper_model_cached() -> bool:
    """Heuristic: any HuggingFace cache entry whose name contains 'whisper'."""
    cache_root = os.path.expanduser("~/.cache/huggingface/hub")
    if not os.path.isdir(cache_root):
        return False
    try:
        for entry in os.listdir(cache_root):
            if "whisper" in entry.lower():
                return True
    except OSError:
        return False
    return False


def _writable(path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".diagnostics_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _database_accessible() -> bool:
    db_path = paths.db_path()
    return db_path.parent.exists() and os.access(db_path.parent, os.W_OK)


@router.get("/api/diagnostics", summary="Environment diagnostics")
async def diagnostics() -> dict[str, Any]:
    ollama_reachable = await _ollama_reachable()

    selected_ollama_model_available = False
    configured_blackhole_device = ""
    try:
        config = load_config()
        configured_blackhole_device = config.audio.blackhole_device_name
        if ollama_reachable and config.summarisation.backend == "ollama":
            selected_ollama_model_available = await _selected_ollama_model_available(
                config.summarisation.ollama_model
            )
    except Exception:
        selected_ollama_model_available = False

    blackhole_candidates = _blackhole_input_candidates()
    configured_blackhole_available = _configured_blackhole_available(
        configured_blackhole_device, blackhole_candidates
    )

    return {
        "platform": "macos" if _is_macos() else sys.platform,
        "apple_silicon": _is_apple_silicon(),
        "blackhole_found": _blackhole_present(),
        "blackhole_candidates": blackhole_candidates,
        "configured_blackhole_device": configured_blackhole_device,
        "configured_blackhole_available": configured_blackhole_available,
        "microphone_available": _microphone_available(),
        "audio_output_devices": _audio_output_devices(),
        "ollama_reachable": ollama_reachable,
        "selected_ollama_model_available": selected_ollama_model_available,
        "mlx_available": _mlx_available(),
        "whisper_model_cached": _whisper_model_cached(),
        "database_accessible": _database_accessible(),
        "logs_dir_writable": _writable(paths.logs_dir()),
        "app_support_dir_writable": _writable(paths.app_support_dir()),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "active_profile": paths.profile_name(),
    }
