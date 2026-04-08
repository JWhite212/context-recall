"""
Audio capture via BlackHole loopback + microphone on macOS.

Records system audio from the BlackHole virtual device and (optionally)
the local microphone, mixing both into a single 16-bit PCM WAV at
16kHz mono — the optimal input format for Whisper-based speech
recognition.

This captures both sides of a conversation: remote participants come
through BlackHole (system audio output loopback) while the local
user's voice comes through the microphone.

Thread safety: start() and stop() are designed to be called from
different threads (e.g., the detector thread calls start/stop while
the audio capture runs on its own thread).
"""

import logging
import os
import queue
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
    Captures audio from the BlackHole virtual device and the local
    microphone, mixes them into a single mono WAV file suitable for
    transcription.
    """

    def __init__(self, config: AudioConfig):
        self._config = config
        self._recording = False
        self._thread: threading.Thread | None = None
        self._output_path: Path | None = None
        self._blackhole_idx: int | None = None
        self._mic_idx: int | None = None

        # Ensure the temp directory exists.
        os.makedirs(config.temp_audio_dir, exist_ok=True)

    def _find_device(self, name: str, kind: str = "input") -> int:
        """
        Locate a device index by name substring. Raises AudioCaptureError
        if not found.
        """
        devices = sd.query_devices()
        for idx, device in enumerate(devices):
            if (
                name.lower() in device["name"].lower()
                and device["max_input_channels"] > 0
            ):
                logger.info(
                    f"Found {kind} device: '{device['name']}' (index {idx})"
                )
                return idx

        available = [
            f"  [{i}] {d['name']} (in={d['max_input_channels']})"
            for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]
        raise AudioCaptureError(
            f"Device '{name}' not found. Available input devices:\n"
            + "\n".join(available)
        )

    def _find_default_input_device(self) -> int | None:
        """Return the index of the system default input device, or None."""
        try:
            info = sd.query_devices(kind="input")
            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                device = sd.query_devices(idx)
                logger.info(
                    f"Using default input device: '{device['name']}' "
                    f"(index {idx})"
                )
                return idx
        except Exception:
            pass
        return None

    def _to_mono(self, data: np.ndarray) -> np.ndarray:
        """Downmix multi-channel audio to a 1-D mono array."""
        if data.ndim == 1:
            return data
        return np.mean(data, axis=1)

    def _record_loop(self) -> None:
        """
        Runs on a background thread. Opens input streams on BlackHole
        and (optionally) the microphone, mixes them, and writes mono
        audio to a WAV file until self._recording is set to False.
        """
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._output_path = (
            Path(self._config.temp_audio_dir) / f"meeting_{timestamp}.wav"
        )

        logger.info(f"Recording to {self._output_path}")

        system_q: queue.Queue[np.ndarray] = queue.Queue()
        mic_q: queue.Queue[np.ndarray] = queue.Queue()
        use_mic = self._config.mic_enabled and self._mic_idx is not None
        mic_vol = max(0.0, min(2.0, self._config.mic_volume))

        if use_mic:
            logger.info(
                f"Dual-source recording: BlackHole (system) + mic "
                f"(volume={mic_vol:.1f})"
            )
        else:
            logger.info("Single-source recording: BlackHole (system) only")

        try:
            with sf.SoundFile(
                str(self._output_path),
                mode="w",
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
                subtype="PCM_16",
            ) as wav_file:

                def system_callback(indata, frames, time_info, status):
                    if status:
                        logger.warning(f"System audio status: {status}")
                    if self._recording:
                        system_q.put(indata.copy())

                def mic_callback(indata, frames, time_info, status):
                    if status:
                        logger.warning(f"Mic audio status: {status}")
                    if self._recording:
                        mic_q.put(indata.copy())

                # Determine mic channel count.
                mic_channels = 1
                if use_mic:
                    mic_info = sd.query_devices(self._mic_idx)
                    mic_channels = min(mic_info["max_input_channels"], 2)

                # Open streams.
                system_stream = sd.InputStream(
                    device=self._blackhole_idx,
                    samplerate=self._config.sample_rate,
                    channels=2,  # BlackHole 2ch always provides stereo.
                    dtype="float32",
                    callback=system_callback,
                    blocksize=1024,
                )

                mic_stream = None
                if use_mic:
                    mic_stream = sd.InputStream(
                        device=self._mic_idx,
                        samplerate=self._config.sample_rate,
                        channels=mic_channels,
                        dtype="float32",
                        callback=mic_callback,
                        blocksize=1024,
                    )

                system_stream.start()
                if mic_stream:
                    mic_stream.start()

                logger.info("Audio stream(s) opened. Capturing...")

                # Mixing buffers.
                system_buf = np.zeros(0, dtype=np.float32)
                mic_buf = np.zeros(0, dtype=np.float32)

                while self._recording:
                    # Drain system audio queue.
                    while not system_q.empty():
                        try:
                            chunk = system_q.get_nowait()
                            system_buf = np.concatenate(
                                [system_buf, self._to_mono(chunk)]
                            )
                        except queue.Empty:
                            break

                    # Drain mic queue.
                    if use_mic:
                        while not mic_q.empty():
                            try:
                                chunk = mic_q.get_nowait()
                                mono = self._to_mono(chunk) * mic_vol
                                mic_buf = np.concatenate([mic_buf, mono])
                            except queue.Empty:
                                break

                    # Mix and write aligned samples.
                    if use_mic:
                        n = min(len(system_buf), len(mic_buf))
                        if n > 0:
                            mixed = system_buf[:n] + mic_buf[:n]
                            mixed = np.clip(mixed, -1.0, 1.0)
                            wav_file.write(mixed.reshape(-1, 1))
                            system_buf = system_buf[n:]
                            mic_buf = mic_buf[n:]
                    else:
                        if len(system_buf) > 0:
                            wav_file.write(system_buf.reshape(-1, 1))
                            system_buf = np.zeros(0, dtype=np.float32)

                    time.sleep(0.05)

                # --- Flush remaining samples after stop ---
                system_stream.stop()
                if mic_stream:
                    mic_stream.stop()

                # Drain any final data from queues.
                while not system_q.empty():
                    try:
                        chunk = system_q.get_nowait()
                        system_buf = np.concatenate(
                            [system_buf, self._to_mono(chunk)]
                        )
                    except queue.Empty:
                        break

                if use_mic:
                    while not mic_q.empty():
                        try:
                            chunk = mic_q.get_nowait()
                            mono = self._to_mono(chunk) * mic_vol
                            mic_buf = np.concatenate([mic_buf, mono])
                        except queue.Empty:
                            break

                # Write remaining mixed audio.
                if use_mic:
                    n = min(len(system_buf), len(mic_buf))
                    if n > 0:
                        mixed = system_buf[:n] + mic_buf[:n]
                        mixed = np.clip(mixed, -1.0, 1.0)
                        wav_file.write(mixed.reshape(-1, 1))
                    # Write any trailing samples from the longer source.
                    remainder = (
                        system_buf[n:] if len(system_buf) > n else mic_buf[n:]
                    )
                    if len(remainder) > 0:
                        wav_file.write(
                            np.clip(remainder, -1.0, 1.0).reshape(-1, 1)
                        )
                else:
                    if len(system_buf) > 0:
                        wav_file.write(system_buf.reshape(-1, 1))

                system_stream.close()
                if mic_stream:
                    mic_stream.close()

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
        Begin recording audio from BlackHole and the microphone.
        Non-blocking: spawns a background thread.
        """
        if self._recording:
            logger.warning("Already recording — ignoring start().")
            return

        self._blackhole_idx = self._find_device(
            self._config.blackhole_device_name, kind="BlackHole"
        )

        # Resolve microphone device.
        self._mic_idx = None
        if self._config.mic_enabled:
            if self._config.mic_device_name:
                try:
                    self._mic_idx = self._find_device(
                        self._config.mic_device_name, kind="microphone"
                    )
                except AudioCaptureError:
                    logger.warning(
                        f"Mic device '{self._config.mic_device_name}' not "
                        f"found. Recording system audio only."
                    )
            else:
                self._mic_idx = self._find_default_input_device()
                if self._mic_idx is None:
                    logger.warning(
                        "No default input device found. "
                        "Recording system audio only."
                    )

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
