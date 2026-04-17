"""
Audio capture via BlackHole loopback + microphone on macOS.

Records system audio from the BlackHole virtual device and (optionally)
the local microphone to separate WAV files, then merges them post-capture
with RMS normalisation. This eliminates clock-drift issues that arise
from real-time mixing of two independent audio devices.

The final output is a single 16-bit PCM WAV at 16kHz mono — the optimal
input format for Whisper-based speech recognition.

Thread safety: start() and stop() are designed to be called from
different threads (e.g., the detector thread calls start/stop while
the audio capture runs on its own thread).
"""

import gc
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from src.utils.config import AudioConfig

logger = logging.getLogger(__name__)

TARGET_RMS_DBFS = -20.0  # Target RMS level for normalisation.
TARGET_RMS_LINEAR = 10.0 ** (TARGET_RMS_DBFS / 20.0)  # Pre-computed linear target.
LEVEL_EMIT_INTERVAL = 0.1  # Seconds between audio level callbacks (~10/sec).
MERGE_CHUNK_SIZE = 16000 * 30  # 30 seconds at 16kHz mono (~1.9MB as float32).


class AudioCaptureError(Exception):
    """Raised when audio capture encounters an unrecoverable error."""


class AudioCapture:
    """
    Captures audio from the BlackHole virtual device and the local
    microphone to separate files, then merges them into a single
    normalised mono WAV file suitable for transcription.
    """

    def __init__(self, config: AudioConfig):
        self._config = config
        self._lock = threading.Lock()
        self._recording = False
        self._thread: threading.Thread | None = None
        self._output_path: Path | None = None
        self._system_path: Path | None = None
        self._mic_path: Path | None = None
        self._blackhole_idx: int | None = None
        self._mic_idx: int | None = None

        # Audio level callback: called with (system_rms, mic_rms) ~10/sec.
        self.on_audio_level: Callable[[float, float], None] | None = None
        self._last_level_time: float = 0.0

        # Lifecycle events for non-blocking stop.
        self._streams_stopped = threading.Event()
        self._merge_complete = threading.Event()

        # Ensure the temp directory exists with owner-only permissions.
        os.makedirs(config.temp_audio_dir, exist_ok=True)
        os.chmod(config.temp_audio_dir, 0o700)

    def _find_device(self, name: str, kind: str = "input") -> int:
        """
        Locate a device index by name substring. Raises AudioCaptureError
        if not found.
        """
        devices = sd.query_devices()
        for idx, device in enumerate(devices):
            if name.lower() in device["name"].lower() and device["max_input_channels"] > 0:
                logger.info(f"Found {kind} device: '{device['name']}' (index {idx})")
                return idx

        available = [
            f"  [{i}] {d['name']} (in={d['max_input_channels']})"
            for i, d in enumerate(devices)
            if d["max_input_channels"] > 0
        ]
        raise AudioCaptureError(
            f"Device '{name}' not found. Available input devices:\n" + "\n".join(available)
        )

    def _find_default_input_device(self) -> int | None:
        """Return the index of the system default input device, or None."""
        try:
            idx = sd.default.device[0]
            if idx is not None and idx >= 0:
                device = sd.query_devices(idx)
                logger.info(f"Using default input device: '{device['name']}' (index {idx})")
                return idx
        except Exception:
            pass
        return None

    def _to_mono(self, data: np.ndarray) -> np.ndarray:
        """Downmix multi-channel audio to a 1-D mono array (always copies)."""
        if data.ndim == 1:
            return data.copy()
        return np.mean(data, axis=1)

    def _record_loop(self) -> None:
        """
        Runs on a background thread. Opens input streams on BlackHole
        and (optionally) the microphone. Each stream writes directly
        to its own WAV file — no real-time mixing, no clock-drift issues.
        """
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        base = Path(self._config.temp_audio_dir)

        self._system_path = base / f"meeting_{timestamp}_system.wav"
        self._output_path = base / f"meeting_{timestamp}.wav"

        use_mic = self._config.mic_enabled and self._mic_idx is not None
        if use_mic:
            self._mic_path = base / f"meeting_{timestamp}_mic.wav"
            logger.info("Dual-source recording: BlackHole (system) + mic")
        else:
            self._mic_path = None
            logger.info("Single-source recording: BlackHole (system) only")

        system_file = None
        mic_file = None
        system_stream = None
        mic_stream = None

        try:
            # Open output WAV files.
            system_file = sf.SoundFile(
                str(self._system_path),
                mode="w",
                samplerate=self._config.sample_rate,
                channels=1,
                subtype="PCM_16",
            )

            if use_mic:
                mic_file = sf.SoundFile(
                    str(self._mic_path),
                    mode="w",
                    samplerate=self._config.sample_rate,
                    channels=1,
                    subtype="PCM_16",
                )

            # Track latest RMS for level metering.
            latest_system_rms = [0.0]
            latest_mic_rms = [0.0]

            # Callbacks write directly to their respective files.
            def system_callback(indata, frames, time_info, status):
                if status:
                    logger.warning("System audio status: %s", status)
                if self._recording:
                    mono = self._to_mono(indata)
                    system_file.write(mono)
                    if self.on_audio_level is not None:
                        latest_system_rms[0] = float(np.sqrt(np.mean(mono**2)))

            def mic_callback(indata, frames, time_info, status):
                if status:
                    logger.warning("Mic audio status: %s", status)
                if self._recording:
                    mono = self._to_mono(indata)
                    mic_file.write(mono)
                    if self.on_audio_level is not None:
                        latest_mic_rms[0] = float(np.sqrt(np.mean(mono**2)))

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

            # Wait until recording is stopped, emitting levels ~10/sec.
            while self._recording:
                now = time.monotonic()
                if self.on_audio_level and now - self._last_level_time >= LEVEL_EMIT_INTERVAL:
                    self._last_level_time = now
                    try:
                        self.on_audio_level(latest_system_rms[0], latest_mic_rms[0])
                    except Exception:
                        pass
                time.sleep(0.05)

        except Exception as e:
            logger.error("Audio capture failed: %s", e, exc_info=True)
            self._output_path = None
            self._recording = False
            return

        finally:
            # Always clean up streams and files, regardless of exit path.
            for stream in (system_stream, mic_stream):
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
            for fh in (system_file, mic_file):
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            self._streams_stopped.set()

        logger.info(
            "System audio: %s (%d KB)",
            self._system_path,
            self._system_path.stat().st_size / 1024,
        )
        if self._mic_path and self._mic_path.exists():
            logger.info(
                "Mic audio:    %s (%d KB)",
                self._mic_path,
                self._mic_path.stat().st_size / 1024,
            )

        # Merge sources into the final output file.
        self._merge_sources()
        self._merge_complete.set()

    def _streaming_rms(self, path: Path) -> float:
        """
        Compute RMS of a WAV file by streaming in chunks, avoiding
        loading the entire file into memory.
        """
        sum_sq = 0.0
        count = 0
        with sf.SoundFile(str(path), mode="r") as f:
            while True:
                chunk = f.read(MERGE_CHUNK_SIZE, dtype="float32")
                if len(chunk) == 0:
                    break
                sum_sq += float(np.sum(chunk**2))
                count += len(chunk)
        if count == 0:
            return 0.0
        return float(np.sqrt(sum_sq / count))

    @staticmethod
    def _rms_dbfs_from_rms(rms: float) -> float:
        """Convert a linear RMS value to dBFS for logging."""
        if rms < 1e-10:
            return -100.0
        return 20.0 * np.log10(rms)

    def _merge_sources(self) -> None:
        """
        Merge the separate source WAV files into a single normalised
        output file using chunked streaming to keep memory usage low.

        Dispatches to single- or dual-source merge based on whether
        a mic recording is available.
        """
        if not self._system_path or not self._system_path.exists():
            logger.error("System audio file missing — cannot merge.")
            self._output_path = None
            return

        has_mic = (
            self._mic_path is not None
            and self._mic_path.exists()
            and self._mic_path.stat().st_size > 44  # WAV header only = empty.
        )

        if has_mic:
            self._merge_dual_source()
        else:
            self._merge_single_source()

        # Clean up source files (keep them if needed for diarisation).
        if not self._config.keep_source_files:
            if self._system_path and self._system_path.exists():
                self._system_path.unlink()
            if self._mic_path and self._mic_path.exists():
                self._mic_path.unlink()
            logger.debug("Deleted temporary source files.")

    def _merge_single_source(self) -> None:
        """Normalise a single system-audio source via chunked streaming."""
        system_rms = self._streaming_rms(self._system_path)
        logger.info(
            "System audio: RMS=%.1f dBFS",
            self._rms_dbfs_from_rms(system_rms),
        )

        if system_rms < 1e-10:
            # Silent — just copy the file as-is.
            shutil.copy2(str(self._system_path), str(self._output_path))
            logger.info("System audio is silent — copied without processing.")
            return

        gain = TARGET_RMS_LINEAR / system_rms

        with sf.SoundFile(str(self._system_path), mode="r") as src:
            with sf.SoundFile(
                str(self._output_path),
                mode="w",
                samplerate=self._config.sample_rate,
                channels=1,
                subtype="PCM_16",
            ) as out:
                while True:
                    chunk = src.read(MERGE_CHUNK_SIZE, dtype="float32")
                    if len(chunk) == 0:
                        break
                    chunk *= gain
                    np.clip(chunk, -1.0, 1.0, out=chunk)
                    out.write(chunk)

        logger.info(
            "Final output: %s (%d KB)",
            self._output_path,
            self._output_path.stat().st_size / 1024,
        )

    def _merge_dual_source(self) -> None:
        """Mix system + mic audio via chunked streaming with RMS normalisation."""
        system_rms = self._streaming_rms(self._system_path)
        mic_rms = self._streaming_rms(self._mic_path)

        logger.info(
            "System audio: RMS=%.1f dBFS",
            self._rms_dbfs_from_rms(system_rms),
        )
        logger.info(
            "Mic audio:    RMS=%.1f dBFS",
            self._rms_dbfs_from_rms(mic_rms),
        )

        system_vol = max(0.0, min(2.0, self._config.system_volume))
        mic_vol = max(0.0, min(2.0, self._config.mic_volume))

        system_gain = (TARGET_RMS_LINEAR / system_rms * system_vol) if system_rms >= 1e-10 else 0.0
        mic_gain = (TARGET_RMS_LINEAR / mic_rms * mic_vol) if mic_rms >= 1e-10 else 0.0

        with (
            sf.SoundFile(str(self._system_path), mode="r") as sys_f,
            sf.SoundFile(str(self._mic_path), mode="r") as mic_f,
            sf.SoundFile(
                str(self._output_path),
                mode="w",
                samplerate=self._config.sample_rate,
                channels=1,
                subtype="PCM_16",
            ) as out,
        ):
            while True:
                sys_chunk = sys_f.read(MERGE_CHUNK_SIZE, dtype="float32")
                mic_chunk = mic_f.read(MERGE_CHUNK_SIZE, dtype="float32")

                if len(sys_chunk) == 0 and len(mic_chunk) == 0:
                    break

                if len(sys_chunk) < len(mic_chunk):
                    sys_chunk = np.pad(sys_chunk, (0, len(mic_chunk) - len(sys_chunk)))
                elif len(mic_chunk) < len(sys_chunk):
                    mic_chunk = np.pad(mic_chunk, (0, len(sys_chunk) - len(mic_chunk)))

                mixed = sys_chunk * system_gain + mic_chunk * mic_gain
                np.clip(mixed, -1.0, 1.0, out=mixed)
                out.write(mixed)

        gc.collect()

        output_size_kb = self._output_path.stat().st_size / 1024
        logger.info(
            "Final output: %s (%d KB)",
            self._output_path,
            output_size_kb,
        )

    @staticmethod
    def _rms_dbfs(audio: np.ndarray) -> float:
        """Calculate RMS level in dBFS."""
        rms = np.sqrt(np.mean(audio**2))
        if rms < 1e-10:
            return -100.0
        return 20.0 * np.log10(rms)

    @staticmethod
    def _normalise_rms(audio: np.ndarray, target_dbfs: float = TARGET_RMS_DBFS) -> np.ndarray:
        """
        Normalise audio in place to a target RMS level in dBFS.
        Returns the same array. No-op if the audio is silent.
        """
        rms = np.sqrt(np.mean(audio**2))
        if rms < 1e-10:
            return audio  # Silent — nothing to normalise.

        target_rms = 10.0 ** (target_dbfs / 20.0)
        gain = target_rms / rms
        audio *= gain
        return audio

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Begin recording audio from BlackHole and the microphone.
        Non-blocking: spawns a background thread.
        """
        with self._lock:
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
                            "Mic device %r not found. Recording system audio only.",
                            self._config.mic_device_name,
                        )
                else:
                    self._mic_idx = self._find_default_input_device()
                    if self._mic_idx is None:
                        logger.warning(
                            "No default input device found. Recording system audio only."
                        )

            self._recording = True
            self._streams_stopped.clear()
            self._merge_complete.clear()
            self._thread = threading.Thread(
                target=self._record_loop,
                name="audio-capture",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, blocking: bool = True) -> Path | None:
        """
        Stop recording and return the path to the final mixed WAV file.

        Args:
            blocking: If True (default), waits for the merge to complete
                before returning. If False, returns immediately after
                streams are closed — call ``wait_for_merge()`` later.
        """
        with self._lock:
            if not self._recording:
                logger.warning("Not recording — ignoring stop().")
                return None
            self._recording = False

        # Wait for audio streams to close (fast — typically <100ms).
        self._streams_stopped.wait(timeout=5)

        if blocking:
            if self._thread:
                self._thread.join(timeout=60)
                self._thread = None

        return self._output_path

    def wait_for_merge(self, timeout: float = 60) -> bool:
        """
        Block until the post-recording merge completes.

        Returns True if the merge finished within *timeout* seconds,
        False if the timeout expired.
        """
        return self._merge_complete.wait(timeout=timeout)

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def output_path(self) -> Path | None:
        return self._output_path

    @property
    def system_audio_path(self) -> Path | None:
        """Path to the separate system audio file (if kept)."""
        if self._system_path and self._system_path.exists():
            return self._system_path
        return None

    @property
    def mic_audio_path(self) -> Path | None:
        """Path to the separate mic audio file (if kept)."""
        if self._mic_path and self._mic_path.exists():
            return self._mic_path
        return None
