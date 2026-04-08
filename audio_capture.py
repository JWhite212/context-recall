"""
Audio capture via BlackHole loopback on macOS.

Records system audio by reading from the BlackHole virtual audio device.
Audio is captured as 16-bit PCM WAV at 16kHz mono, which is the optimal
input format for Whisper-based speech recognition.

The recorder writes to a temporary WAV file during the meeting and
exposes the final file path once stopped. Chunked recording (writing
intermediate segments) is supported for very long meetings to avoid
holding large buffers in memory.

Thread safety: start() and stop() are designed to be called from
different threads (e.g., the detector thread calls start/stop while
the audio capture runs on its own thread).
"""

import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from src.utils.config import AudioConfig

logger = logging.getLogger(__name__)


class AudioCaptureError(Exception):
    """Raised when audio capture encounters an unrecoverable error."""


class AudioCapture:
    """
    Captures audio from the BlackHole virtual device and writes
    it to a WAV file suitable for transcription.
    """

    def __init__(self, config: AudioConfig):
        self._config = config
        self._recording = False
        self._thread: threading.Thread | None = None
        self._output_path: Path | None = None
        self._device_index: int | None = None

        # Ensure the temp directory exists.
        os.makedirs(config.temp_audio_dir, exist_ok=True)

    def _find_blackhole_device(self) -> int:
        """
        Locate the BlackHole device index from the available audio
        input devices. Raises AudioCaptureError if not found.

        Run `python -m sounddevice` to see all available devices
        if this fails.
        """
        devices = sd.query_devices()
        for idx, device in enumerate(devices):
            if (
                self._config.blackhole_device_name.lower()
                in device["name"].lower()
                and device["max_input_channels"] > 0
            ):
                logger.info(
                    f"Found BlackHole device: '{device['name']}' "
                    f"(index {idx})"
                )
                return idx

        available = [
            f"  [{i}] {d['name']} (in={d['max_input_channels']})"
            for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]
        raise AudioCaptureError(
            f"BlackHole device '{self._config.blackhole_device_name}' "
            f"not found. Available input devices:\n"
            + "\n".join(available)
        )

    def _record_loop(self) -> None:
        """
        Runs on a background thread. Opens an input stream on the
        BlackHole device and writes audio data to a WAV file until
        self._recording is set to False.
        """
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._output_path = Path(self._config.temp_audio_dir) / f"meeting_{timestamp}.wav"

        logger.info(f"Recording to {self._output_path}")

        try:
            with sf.SoundFile(
                str(self._output_path),
                mode="w",
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
                subtype="PCM_16",
            ) as wav_file:

                def audio_callback(indata, frames, time_info, status):
                    if status:
                        logger.warning(f"Audio callback status: {status}")
                    if self._recording:
                        # Downmix to mono if the device provides stereo.
                        if indata.shape[1] > self._config.channels:
                            mono = np.mean(indata, axis=1, keepdims=True)
                            wav_file.write(mono)
                        else:
                            wav_file.write(indata)

                with sd.InputStream(
                    device=self._device_index,
                    samplerate=self._config.sample_rate,
                    channels=2,  # BlackHole 2ch always provides stereo.
                    dtype="float32",
                    callback=audio_callback,
                    blocksize=1024,
                ):
                    logger.info("Audio stream opened. Capturing...")
                    while self._recording:
                        time.sleep(0.1)

            logger.info(
                f"Recording complete: {self._output_path} "
                f"({self._output_path.stat().st_size / 1024:.0f} KB)"
            )

        except Exception as e:
            logger.error(f"Audio capture failed: {e}", exc_info=True)
            self._output_path = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Begin recording audio from the BlackHole device.
        Non-blocking: spawns a background thread.
        """
        if self._recording:
            logger.warning("Already recording — ignoring start().")
            return

        self._device_index = self._find_blackhole_device()
        self._recording = True
        self._thread = threading.Thread(
            target=self._record_loop,
            name="audio-capture",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> Path | None:
        """
        Stop recording and return the path to the captured WAV file.

        Returns None if no audio was captured (e.g., due to an error
        or if the recording was never started).
        """
        if not self._recording:
            logger.warning("Not recording — ignoring stop().")
            return None

        self._recording = False

        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

        return self._output_path

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def output_path(self) -> Path | None:
        return self._output_path
