"""
Support bundle endpoint.

GET /api/support_bundle — returns a zip archive of safe diagnostic
content the user can attach to a bug report. The bundle is carefully
curated to avoid leaking sensitive data:

* Secrets in the loaded config are redacted.
* The auth token, SQLite database, and audio recordings are
  deliberately excluded — only their on-disk paths are reported.
* Environment variable values are never included.

The endpoint is read-only and side-effect free.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import platform as _platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.utils import paths
from src.utils.config import load_config

logger = logging.getLogger("contextrecall.api.support_bundle")

router = APIRouter()

APP_VERSION = "0.1.0"

# Keys whose values should be replaced with "[REDACTED]" in the
# config dump. Matches anywhere in the key (case-insensitive) for
# common secret-bearing names like ``anthropic_api_key`` or
# ``smtp_password``.
_REDACT_KEY_RE = re.compile(r"(?i)token|key|secret|password|pwd|cred")

_REDACTED_PLACEHOLDER = "[REDACTED]"

# Ceiling on log content included in the bundle and per-file size cap.
_LOG_TAIL_BYTES = 200 * 1024  # 200 KB
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


# Resolve the diagnostics handler lazily so this module imports cleanly
# even if the diagnostics route module isn't present (e.g. during a
# partial rollout where unit 7 lands after unit 8).
def _diagnostics_handler() -> Callable[[], Awaitable[dict[str, Any]]]:
    try:
        from src.api.routes.diagnostics import diagnostics  # type: ignore[import]

        return diagnostics
    except Exception:  # pragma: no cover — only hit if diagnostics missing

        async def _missing() -> dict[str, Any]:
            return {"error": "diagnostics module not available"}

        return _missing


def _redact(obj: Any) -> Any:
    """Recursively redact sensitive values in dicts/lists.

    Any dict key matching ``_REDACT_KEY_RE`` has its value replaced with
    the literal string ``[REDACTED]`` regardless of the value's type.
    Other containers are walked recursively so nested structures stay
    redacted. Scalars are returned unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: (_REDACTED_PLACEHOLDER if _REDACT_KEY_RE.search(str(k)) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(item) for item in obj]
    return obj


def _audio_devices_summary() -> list[dict[str, Any]]:
    """Return a minimal description of audio devices, never raising."""
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        return [
            {
                "name": str(dev.get("name", "")),
                "max_input_channels": int(dev.get("max_input_channels", 0)),
                "max_output_channels": int(dev.get("max_output_channels", 0)),
            }
            for dev in devices
        ]
    except Exception as exc:  # pragma: no cover — best-effort only
        logger.debug("audio device enumeration failed: %s", exc)
        return []


def _collect_recent_logs() -> str:
    """Return up to 200 KB of tail content across `*.log` files in logs_dir.

    Files larger than 5 MB are skipped silently. Sections are separated by
    a header line so the operator can tell which file each block came from.
    """
    log_dir = paths.logs_dir()
    if not log_dir.is_dir():
        return ""

    try:
        log_files = list(log_dir.glob("*.log"))
    except OSError:
        return ""

    # Walk newest-first so the most useful tail wins when the budget runs out.
    log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    chunks: list[str] = []
    remaining = _LOG_TAIL_BYTES
    for log_file in log_files:
        if remaining <= 0:
            break
        try:
            size = log_file.stat().st_size
            if size > _LOG_FILE_MAX_BYTES:
                continue
            with log_file.open("rb") as fh:
                if size > remaining:
                    fh.seek(size - remaining)
                data = fh.read(remaining)
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        chunks.append(f"===== {log_file.name} (last {len(data)} bytes) =====\n{text}")
        remaining -= len(data)

    return "\n\n".join(chunks)


def _paths_summary() -> dict[str, str]:
    """String paths only — no file contents read or included."""
    return {
        "app_support_dir": str(paths.app_support_dir()),
        "logs_dir": str(paths.logs_dir()),
        "cache_dir": str(paths.cache_dir()),
        "db_path": str(paths.db_path()),
        "audio_dir": str(paths.audio_dir()),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile_name() -> str:
    """Return the active profile name, or ``prod`` if the helper is absent."""
    fn = getattr(paths, "profile_name", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:
            pass
    return "prod"


async def _build_bundle() -> bytes:
    """Assemble the zip bundle and return its bytes."""
    metadata = {
        "app_version": APP_VERSION,
        "platform": sys.platform,
        "platform_release": _platform.release(),
        "machine": _platform.machine(),
        "python_version": sys.version.split(" ", 1)[0],
        "profile": _profile_name(),
        "generated_at": _utc_now_iso(),
    }

    try:
        redacted_config = _redact(dataclasses.asdict(load_config()))
    except Exception as exc:
        logger.warning("config snapshot failed: %s", exc)
        redacted_config = {"error": "could not load config"}

    try:
        diagnostics_payload = await _diagnostics_handler()()
    except Exception as exc:
        logger.warning("diagnostics call failed: %s", exc)
        diagnostics_payload = {"error": str(exc)}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, indent=2, sort_keys=True))
        zf.writestr(
            "config.redacted.json",
            json.dumps(redacted_config, indent=2, sort_keys=True, default=str),
        )
        zf.writestr(
            "diagnostics.json",
            json.dumps(diagnostics_payload, indent=2, sort_keys=True, default=str),
        )
        zf.writestr(
            "audio_devices.json",
            json.dumps(_audio_devices_summary(), indent=2, sort_keys=True),
        )
        zf.writestr("recent_logs.txt", _collect_recent_logs())
        zf.writestr("paths.json", json.dumps(_paths_summary(), indent=2, sort_keys=True))

    return buffer.getvalue()


@router.get("/api/support_bundle", summary="Generate a redacted diagnostic support bundle")
async def support_bundle() -> StreamingResponse:
    """Return a zip with redacted config, diagnostics, and recent logs."""
    payload = await _build_bundle()
    filename = f"context-recall-support-bundle-{_utc_now_iso()}.zip"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
