"""
macOS Screen Recording (TCC) introspection and request via CoreGraphics.

ScreenCaptureKit captures system audio through the **Screen Recording** TCC
service. A launchd background daemon never registers itself in the Screen
Recording list by merely attempting a capture — the ``SCStream`` just fails
with ``-3801`` (observed 2026-07-16) — and its nested-app code identity
(``dev.jamiewhite.contextrecall.daemon``) cannot be added via the System
Settings "+" button, because the picker resolves the nested daemon app to the
*outer* bundle (``dev.jamiewhite.contextrecall``) instead.

``CGRequestScreenCaptureAccess()`` registers **this process's** code identity
in the Screen Recording list so the user can toggle it on. Unlike the
microphone request (which tccd will KILL a launchd daemon for issuing without
an ``NSMicrophoneUsageDescription``), Screen Recording has no usage-description
requirement, so the call is safe from the frozen daemon.

CoreGraphics is reached via ctypes (no pyobjc dependency), and every entry
point degrades to ``UNKNOWN`` / ``None`` on non-macOS platforms or on binding
failure — introspection must never block a recording that might have worked.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

GRANTED = "granted"
DENIED = "denied"
UNKNOWN = "unknown"

_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


def _cg() -> ctypes.CDLL | None:
    """Resolve the CoreGraphics screen-capture-access symbols, or None."""
    if sys.platform != "darwin":
        return None
    try:
        cg = ctypes.CDLL(_CG_PATH)
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return cg
    except Exception:
        logger.debug("CoreGraphics screen-capture binding unavailable", exc_info=True)
        return None


def screen_recording_status() -> str:
    """Screen Recording status for THIS process. Never prompts.

    ``CGPreflightScreenCaptureAccess`` cannot distinguish "denied" from
    "not yet requested" — both surface as False — so both map to ``DENIED``.
    Returns ``UNKNOWN`` off-darwin or on binding failure.
    """
    cg = _cg()
    if cg is None:
        return UNKNOWN
    try:
        return GRANTED if cg.CGPreflightScreenCaptureAccess() else DENIED
    except Exception:
        logger.debug("CGPreflightScreenCaptureAccess failed", exc_info=True)
        return UNKNOWN


def request_screen_recording_access() -> bool | None:
    """Register this process's identity in the Screen Recording list + request.

    ``CGRequestScreenCaptureAccess`` adds this code identity to System
    Settings → Privacy & Security → Screen Recording (even when it returns
    False, i.e. not-yet-granted), which is the whole point: the user can then
    toggle the daemon on. Returns True/False for granted/not, or ``None``
    off-darwin or on binding failure.
    """
    cg = _cg()
    if cg is None:
        return None
    try:
        return bool(cg.CGRequestScreenCaptureAccess())
    except Exception:
        logger.debug("CGRequestScreenCaptureAccess failed", exc_info=True)
        return None
