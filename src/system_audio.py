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
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from src.audio_capture import AudioCaptureError
from src.utils.config import AudioConfig

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


class SystemAudioBackend:
    """Interface for a swappable system-audio source writing _system.wav.

    Subclasses own device/helper lifecycle and expose the current RMS plus
    forwarded live-audio / stream-status callbacks. AudioCapture drives one of
    these while owning the mic stream, merge, and pipeline.
    """

    def __init__(self) -> None:
        self.on_audio_data: Callable[[np.ndarray], None] | None = None
        self.on_stream_status: Callable[[str, str], None] | None = None
        self.last_error: AudioCaptureError | None = None
        self.last_warning: str | None = None
        self._latest_rms: float = 0.0

    @property
    def latest_rms(self) -> float:
        return self._latest_rms

    def start(self, output_path: Path) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class BlackHoleSystemCapture(SystemAudioBackend):
    """System source backed by the BlackHole loopback via sounddevice.

    Behaviourally identical to the pre-refactor AudioCapture system path: opens
    a 2ch float32 InputStream on the BlackHole device, downmixes to mono, writes
    16 kHz mono PCM-16, and forwards live audio + RMS.
    """

    def __init__(self, config: AudioConfig) -> None:
        super().__init__()
        self._config = config
        self._stream: sd.InputStream | None = None
        self._file: sf.SoundFile | None = None
        self._running = False

    def _find_blackhole(self, name: str) -> int:
        devices = sd.query_devices()
        for idx, device in enumerate(devices):
            if name.lower() in device["name"].lower() and device["max_input_channels"] > 0:
                logger.info("Found BlackHole device: '%s' (index %d)", device["name"], idx)
                return idx
        raise AudioCaptureError(f"BlackHole device '{name}' not found")

    def start(self, output_path: Path) -> None:
        try:
            idx = self._find_blackhole(self._config.blackhole_device_name)
        except AudioCaptureError as e:
            logger.error("BlackHole capture unavailable: %s", e)
            self.last_error = e
            return

        self._file = sf.SoundFile(
            str(output_path),
            mode="w",
            samplerate=self._config.sample_rate,
            channels=1,
            subtype="PCM_16",
        )
        self._running = True

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning("System audio status: %s", status)
                if self.on_stream_status is not None:
                    try:
                        self.on_stream_status("system", str(status))
                    except Exception:
                        pass
            if not self._running:
                return
            mono = indata.copy() if indata.ndim == 1 else np.mean(indata, axis=1)
            self._file.write(mono)
            if self.on_audio_data is not None:
                try:
                    self.on_audio_data(mono)
                except Exception:
                    pass
            self._latest_rms = float(np.sqrt(np.mean(mono**2)))

        self._stream = sd.InputStream(
            device=idx,
            samplerate=self._config.sample_rate,
            channels=2,
            dtype="float32",
            callback=callback,
            blocksize=1024,
        )
        self._stream.start()
        logger.info("BlackHole system capture started -> %s", output_path)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
