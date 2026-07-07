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

import logging
from typing import Iterable, Sequence

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


def is_virtual_input(name: str) -> bool:
    """True if the device name identifies a loopback/virtual device."""
    lowered = name.lower()
    return any(pattern in lowered for pattern in VIRTUAL_INPUT_PATTERNS)


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
