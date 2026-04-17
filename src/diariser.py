"""
Speaker diarisation backends.

Supports two backends:
- **energy** (default): Compares RMS energy levels between the system audio
  (remote participants) and microphone audio (local user). No ML
  dependencies — just signal-level comparison leveraging the dual-source
  recording architecture. Requires separate source WAV files from audio
  capture (audio.keep_source_files must be true, set automatically when
  diarisation is enabled).
- **pyannote**: Uses pyannote.audio's pretrained speaker diarisation
  pipeline to identify individual speakers. Requires a HuggingFace
  token and the ``pyannote.audio`` package (heavy, optional dependency).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import soundfile as sf

from src.transcriber import Transcript
from src.utils.config import DiarisationConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class DiariserBackend(Protocol):
    """Protocol that all diarisation backends must implement."""

    def diarise(self, transcript: Transcript, audio_path: Path) -> Transcript:
        """Label each segment with a speaker identifier."""
        ...


class EnergyDiariser:
    """
    Labels transcript segments with speaker identifiers by comparing
    energy levels between mic and system audio recordings.
    """

    def __init__(self, config: DiarisationConfig):
        self._config = config

    def diarise(
        self,
        transcript: Transcript,
        audio_path: Path,
        *,
        mic_audio_path: Path | None = None,
    ) -> Transcript:
        """
        Label each segment in *transcript* with a speaker identifier.

        Compares RMS energy in the corresponding time window of each
        source file to determine who was speaking. Uses seek-based
        reading to avoid loading entire files into memory.

        Note:
            Mutates *transcript* in place. The same object is returned
            for method-chaining convenience.
        """
        if mic_audio_path is None:
            raise FileNotFoundError(
                "EnergyDiariser requires mic_audio_path for dual-source comparison"
            )
        for path, label in [
            (audio_path, "system audio"),
            (mic_audio_path, "mic audio"),
        ]:
            if not path.exists():
                raise FileNotFoundError(f"Diarisation requires {label} file: {path}")

        threshold = self._config.energy_ratio_threshold
        me = self._config.speaker_name
        remote = self._config.remote_label

        with (
            sf.SoundFile(str(audio_path)) as system_sf,
            sf.SoundFile(str(mic_audio_path)) as mic_sf,
        ):
            if system_sf.samplerate != mic_sf.samplerate:
                raise ValueError(
                    f"Sample rate mismatch: system={system_sf.samplerate}Hz, "
                    f"mic={mic_sf.samplerate}Hz"
                )

            sample_rate = system_sf.samplerate

            for segment in transcript.segments:
                start_sample = int(segment.start * sample_rate)
                end_sample = int(segment.end * sample_rate)
                num_frames = end_sample - start_sample

                if (
                    start_sample >= system_sf.frames
                    or start_sample >= mic_sf.frames
                    or num_frames <= 0
                ):
                    segment.speaker = ""
                    continue

                system_sf.seek(start_sample)
                sys_window = system_sf.read(frames=num_frames, dtype="float32")

                mic_sf.seek(start_sample)
                mic_window = mic_sf.read(frames=num_frames, dtype="float32")

                sys_rms = self._rms(sys_window)
                mic_rms = self._rms(mic_window)

                if mic_rms > sys_rms * threshold:
                    segment.speaker = me
                elif sys_rms > mic_rms * threshold:
                    segment.speaker = remote
                else:
                    segment.speaker = f"{me} + {remote}"

        # Log summary.
        counts: dict[str, int] = {}
        for seg in transcript.segments:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
        logger.info("Diarisation complete: %s", counts)

        return transcript

    @staticmethod
    def _rms(audio: np.ndarray) -> float:
        """Calculate RMS of an audio array."""
        if len(audio) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio**2)))


# Backward-compatible alias.
Diariser = EnergyDiariser


def create_diariser(config: DiarisationConfig) -> DiariserBackend:
    """Factory: return the correct diariser backend for *config.backend*."""
    backend = config.backend
    if backend == "energy":
        return EnergyDiariser(config)
    if backend == "pyannote":
        try:
            import pyannote.audio  # noqa: F401
        except ImportError:
            raise ValueError(
                "Pyannote backend requires 'pyannote.audio' and its dependencies. "
                "Install with: pip install pyannote.audio"
            ) from None
        from src.pyannote_diariser import PyAnnoteDiariser

        return PyAnnoteDiariser(config)
    raise ValueError(
        f"Unknown diarisation backend: {backend!r}. Supported backends: 'energy', 'pyannote'."
    )
