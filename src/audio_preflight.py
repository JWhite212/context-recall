"""
Pre-flight audio device + permission checks.

Inspects the audio environment BEFORE the recording pipeline opens
streams, so the orchestrator can refuse to start (or warn the user)
when something is obviously broken — BlackHole missing, the configured
microphone unopenable, microphone permission denied.

This module is intentionally read-only and side-effect free apart from
the briefly-opened mic input stream used to detect permission denial.
It is safe to call repeatedly (e.g. from a /api/preflight endpoint).
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Sequence

import sounddevice as sd

from src.utils.config import AudioConfig

logger = logging.getLogger("contextrecall.audio_preflight")

# How long to hold the mic stream open while probing. Short enough to be
# imperceptible, long enough for PortAudio to surface a permission error.
MIC_PROBE_SECONDS = 0.2


@dataclass
class PreflightReport:
    """Result of a pre-flight audio check.

    Attributes:
        blackhole_present: True if any device with 'blackhole' in its
            name (case-insensitive) is visible.
        blackhole_input_candidates: Names of input devices containing
            'blackhole' — exactly the values that would satisfy
            ``AudioConfig.blackhole_device_name`` substring matching.
        mic_openable: True if a short ``sd.InputStream`` opened against
            the configured (or default) microphone without raising.
        microphone_permission_likely: Proxy for macOS microphone
            permission. True if the mic was openable, False otherwise.
        default_input_index: System default input device index, or None
            if no default is configured / accessible.
        warnings: Non-fatal issues the user should be aware of.
        errors: Hard failures that should abort the recording start.
    """

    blackhole_present: bool = False
    blackhole_input_candidates: list[str] = field(default_factory=list)
    mic_openable: bool = False
    microphone_permission_likely: bool = False
    default_input_index: int | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _resolve_default_input_index() -> int | None:
    """Mirror the defensive behaviour in routes/devices.py."""
    try:
        default_input = sd.default.device[0]
    except Exception:
        return None
    if default_input is None or default_input < 0:
        return None
    return int(default_input)


def _find_input_index_by_name(devices: Sequence[dict], name: str) -> int | None:
    """Substring-match input device name (mirrors AudioCapture._find_device)."""
    if not name:
        return None
    needle = name.lower()
    for idx, dev in enumerate(devices):
        if needle in str(dev.get("name", "")).lower() and dev.get("max_input_channels", 0) > 0:
            return idx
    return None


def _probe_input_stream(
    device_index: int | None,
    sample_rate: int,
    channels: int = 1,
) -> tuple[bool, str | None]:
    """Briefly open an ``InputStream`` to confirm the device is usable.

    Returns ``(openable, reason)``. On success ``reason`` is None; on
    failure ``reason`` is a short human-readable explanation suitable
    for a pipeline.error / pipeline.warning event.
    """
    try:
        stream = sd.InputStream(
            device=device_index,
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            blocksize=1024,
        )
    except Exception as e:
        return False, f"Failed to open microphone: {e}"

    try:
        stream.start()
        time.sleep(MIC_PROBE_SECONDS)
    except Exception as e:
        try:
            stream.close()
        except Exception:
            pass
        return False, f"Microphone stream failed to start: {e}"

    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass
    return True, None


def run_preflight(config: AudioConfig) -> PreflightReport:
    """Inspect the audio environment for the upcoming recording.

    See module docstring. The returned report carries both ``warnings``
    (recoverable degradations) and ``errors`` (hard stops); callers
    should refuse to start a recording when ``errors`` is non-empty.
    """
    report = PreflightReport()

    # 1. Enumerate devices.
    try:
        devices = sd.query_devices()
    except Exception as e:
        # Without a device list we cannot do any other check — but we
        # also cannot tell whether this is a transient PortAudio init
        # blip or a permanent failure. Treat as an error to be safe.
        report.errors.append(f"Unable to query audio devices: {e}")
        return report

    # 2. BlackHole presence + input candidates.
    blackhole_candidates: list[str] = []
    blackhole_any = False
    for dev in devices:
        name = str(dev.get("name", ""))
        if "blackhole" in name.lower():
            blackhole_any = True
            if dev.get("max_input_channels", 0) > 0:
                blackhole_candidates.append(name)

    report.blackhole_present = blackhole_any
    report.blackhole_input_candidates = blackhole_candidates

    if not blackhole_any:
        report.errors.append(
            "BlackHole virtual audio driver is not installed. System audio "
            "cannot be captured without it — install BlackHole 2ch from "
            "https://existential.audio/blackhole/ and route your system "
            "output to it via a Multi-Output Device in Audio MIDI Setup."
        )
    elif not blackhole_candidates:
        # BlackHole is visible but only as an output — unusable for capture.
        report.errors.append(
            "BlackHole is installed but no BlackHole input device is "
            "available. Re-install BlackHole 2ch or check Audio MIDI Setup."
        )
    else:
        # Confirm the configured name actually matches one of the
        # candidates the capture path would accept.
        configured = config.blackhole_device_name or ""
        if configured and not any(configured.lower() in c.lower() for c in blackhole_candidates):
            report.warnings.append(
                f"Configured BlackHole device {configured!r} does not match any "
                f"installed input device. Available BlackHole inputs: "
                f"{', '.join(blackhole_candidates)}."
            )

    # 3. Default input index.
    report.default_input_index = _resolve_default_input_index()

    # 4. Microphone openability (proxy for macOS permission).
    # Mic disabled / not found / probe failure all leave mic_openable and
    # microphone_permission_likely at their dataclass defaults of False.
    if not config.mic_enabled:
        return report

    if config.mic_device_name:
        mic_index = _find_input_index_by_name(devices, config.mic_device_name)
        if mic_index is None:
            report.warnings.append(
                f"Configured microphone {config.mic_device_name!r} was not "
                f"found. Recording will fall back to system audio only."
            )
    else:
        mic_index = report.default_input_index
        if mic_index is None:
            report.warnings.append(
                "No default microphone is available. Recording will fall "
                "back to system audio only — open System Settings → "
                "Privacy & Security → Microphone to grant access, or pick "
                "a device in Settings."
            )

    if mic_index is None:
        return report

    openable, reason = _probe_input_stream(mic_index, sample_rate=config.sample_rate)
    report.mic_openable = openable
    report.microphone_permission_likely = openable
    if not openable:
        # Likely permission denial on macOS. Surface as a warning so the
        # system-audio path can still run.
        report.warnings.append(
            (reason or "Microphone could not be opened.")
            + " On macOS, grant microphone access in System Settings "
            "→ Privacy & Security → Microphone."
        )

    return report
