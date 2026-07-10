"""BlackHole-only system-audio RMS monitor for calendar auto-arm.

While an event is armed (but nothing is recording yet), this opens a
single BlackHole input stream and watches system-audio level. ``active()``
returns True once the RMS stays above ``threshold_dbfs`` for
``sustain_seconds`` — the "a meeting actually started" signal. No files are
written; the stream is mutually exclusive with the real capture (both read
BlackHole), so the controller closes it before recording begins.

The sustain logic (``observe``/``active``) is a pure, time-injected state
machine tested without any real audio. Stream I/O in ``start``/``stop`` is
guarded: if the device can't be opened, ``active()`` simply stays False and
auto-arm falls back to the process signal.
"""

import logging
import math
import time

import numpy as np
import sounddevice as sd

from src.audio_devices import refresh_input_devices

logger = logging.getLogger(__name__)


class AudioMonitor:
    def __init__(
        self,
        *,
        blackhole_device_name: str,
        sample_rate: int,
        threshold_dbfs: float = -45.0,
        sustain_seconds: float = 3.0,
        clock=time.monotonic,
    ) -> None:
        self._device_name = blackhole_device_name
        self._sample_rate = sample_rate
        self._threshold_dbfs = threshold_dbfs
        self._sustain_seconds = sustain_seconds
        self._clock = clock

        self._stream = None
        self._above_since: float | None = None
        self._active: bool = False

    # ------------------------------------------------------------------
    # Pure sustain core (unit-tested)
    # ------------------------------------------------------------------

    def observe(self, rms: float, now: float) -> None:
        """Record one linear-RMS sample and update the sustain latch."""
        dbfs = -100.0 if rms < 1e-10 else 20.0 * math.log10(rms)
        if dbfs >= self._threshold_dbfs:
            if self._above_since is None:
                self._above_since = now
            self._active = (now - self._above_since) >= self._sustain_seconds
        else:
            self._above_since = None
            self._active = False

    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # Stream I/O (guarded; not exercised in unit tests)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the BlackHole input stream (best-effort)."""
        if self._stream is not None:
            return
        try:
            # Un-freeze PortAudio's device table (safe: no stream open yet).
            refresh_input_devices()
            device_idx = self._find_blackhole_index()

            def _callback(indata, frames, time_info, status):
                if status:
                    logger.warning("Auto-arm monitor audio status: %s", status)
                mono = np.mean(indata, axis=1) if indata.ndim > 1 else indata
                rms = float(np.sqrt(np.mean(mono**2)))
                self.observe(rms, self._clock())

            self._stream = sd.InputStream(
                device=device_idx,
                samplerate=self._sample_rate,
                channels=2,  # BlackHole always provides stereo.
                dtype="float32",
                callback=_callback,
                blocksize=1024,
            )
            self._stream.start()
            logger.info("Auto-arm audio monitor opened on '%s'.", self._device_name)
        except Exception:
            logger.warning(
                "Auto-arm audio monitor failed to open — relying on the process signal only.",
                exc_info=True,
            )
            self._stream = None
            self._reset()

    def stop(self) -> None:
        """Close the stream and reset sustain state."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._reset()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        self._above_since = None
        self._active = False

    def _find_blackhole_index(self) -> int:
        """Substring match over input devices (like AudioCapture._find_device)."""
        devices = sd.query_devices()
        name = self._device_name.lower()
        for idx, device in enumerate(devices):
            if name in device["name"].lower() and device["max_input_channels"] > 0:
                return idx
        raise RuntimeError(f"Auto-arm monitor device '{self._device_name}' not found")
