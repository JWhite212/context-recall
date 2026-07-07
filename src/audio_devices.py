"""
Shared audio input-device resolution.

The daemon must never open its "microphone" stream on a loopback or
virtual device: on macOS the default input device can legitimately be
BlackHole (users set it while configuring routing, and some tools set it
as a side effect), in which case blindly trusting the default records
the silent loopback twice and captures no real microphone at all.

Used by both AudioCapture (recording) and audio_preflight (checks) so
the two always agree on what counts as a usable microphone.
"""

from __future__ import annotations

import difflib
import logging
from typing import Iterable, Sequence

import sounddevice as sd

logger = logging.getLogger(__name__)

# Name fragments that identify loopback / virtual / composite devices
# which must never be auto-selected as the microphone. Explicit user
# configuration (audio.mic_device_name) bypasses this list.
VIRTUAL_INPUT_PATTERNS = (
    "blackhole",
    "loopback",
    "soundflower",
    "teams audio",
    "zoomaudiodevice",
    "aggregate",
    "multi-output",
)


def refresh_input_devices() -> None:
    """Re-initialise PortAudio so the device table reflects current hardware.

    PortAudio snapshots devices at initialisation, so a long-running daemon
    never observes devices being plugged/unplugged or default-input changes
    (observed in production: the daemon still saw a stale default input
    hours after the user switched devices). MUST NOT be called while any
    stream is open — re-initialisation invalidates open streams.
    """
    try:
        sd._terminate()
    except Exception:
        logger.warning("PortAudio terminate failed during device refresh", exc_info=True)
    try:
        sd._initialize()
    except Exception:
        logger.warning("PortAudio initialise failed during device refresh", exc_info=True)


def is_virtual_input(name: str) -> bool:
    """True if the device name identifies a loopback/virtual device."""
    lowered = name.lower()
    return any(pattern in lowered for pattern in VIRTUAL_INPUT_PATTERNS)


def resolve_named_input_index(
    devices: Sequence[dict],
    name: str,
) -> tuple[int | None, str | None]:
    """Resolve an explicitly-configured input device name.

    Exact (case-insensitive) substring match first — the contract the
    capture path has always used. When that fails, fall back to a fuzzy
    match against real (non-virtual) input devices so a typo in Settings
    degrades to a warning instead of silently costing the microphone
    (production 2026-07-07: configured 'Jabre Link 390', hardware
    'Jabra Link 390' — every recording fell back to system-audio-only).

    Returns ``(index, note)``: ``note`` is None on an exact match, a
    human-readable substitution message on a fuzzy match, and
    ``(None, None)`` when nothing matches.
    """
    if not name:
        return None, None

    needle = name.lower()
    inputs = [
        (idx, str(dev.get("name", "")))
        for idx, dev in enumerate(devices)
        if dev.get("max_input_channels", 0) > 0
    ]
    for idx, dev_name in inputs:
        if needle in dev_name.lower():
            return idx, None

    best: tuple[float, int, str] | None = None
    for idx, dev_name in inputs:
        if is_virtual_input(dev_name):
            continue  # A typo must never land on the loopback.
        ratio = difflib.SequenceMatcher(None, needle, dev_name.lower()).ratio()
        if ratio >= 0.75 and (best is None or ratio > best[0]):
            best = (ratio, idx, dev_name)
    if best is None:
        return None, None

    _, idx, dev_name = best
    note = (
        f"Configured microphone {name!r} was not found — using the closest "
        f"match {dev_name!r}. Update the device name in Settings to silence "
        f"this warning."
    )
    return idx, note


def resolve_default_mic_index(
    devices: Sequence[dict],
    default_index: int | None,
    exclude: Iterable[int] = (),
) -> int | None:
    """Pick the input device to use as the microphone.

    Preference order:
      1. The system default input, if it is a real (non-virtual) input
         device and not excluded.
      2. The first real input device whose name mentions a microphone.
      3. The first real input device of any name.

    Returns None when no real input device exists — the caller should
    fall back to system-audio-only recording with a warning.
    """
    excluded = set(exclude)

    def _usable(idx: int) -> bool:
        device = devices[idx]
        return (
            idx not in excluded
            and device.get("max_input_channels", 0) > 0
            and not is_virtual_input(str(device.get("name", "")))
        )

    if default_index is not None and 0 <= default_index < len(devices):
        if _usable(default_index):
            return default_index

    candidates = [idx for idx in range(len(devices)) if _usable(idx)]
    if not candidates:
        return None

    for idx in candidates:
        if "mic" in str(devices[idx].get("name", "")).lower():
            return idx
    return candidates[0]
