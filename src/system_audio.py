"""System-audio capture backends (BlackHole loopback / ScreenCaptureKit).

The daemon captures *system output* (remote meeting participants) through one
of two interchangeable backends, both writing ``meeting_<ts>_system.wav`` as
16 kHz mono PCM-16. ScreenCaptureKit uses the Screen Recording TCC service,
which keeps working on macOS betas where the Microphone service (and thus the
BlackHole input) is broken. See
docs/superpowers/specs/2026-07-16-screencapturekit-system-audio-design.md.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HELPER_NAME = "sck-audio-capture"


def resolve_helper_path() -> Path | None:
    """Locate the bundled/dev SCK helper binary, or None if unavailable.

    Frozen (.app) builds ship it at Contents/Resources/<HELPER_NAME>; dev runs
    use the output of scripts/build_sck_helper.sh. Returns None when the binary
    is missing or not executable, so callers can degrade to BlackHole.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent.parent / "Resources" / HELPER_NAME
    else:
        candidate = (
            Path(__file__).resolve().parent.parent
            / "macos"
            / "sck-audio-capture"
            / ".build"
            / HELPER_NAME
        )
    if candidate.exists() and os.access(candidate, os.X_OK):
        return candidate
    return None
