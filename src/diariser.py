"""
Energy-based speaker diarisation.

Compares RMS energy levels between the system audio (remote participants)
and microphone audio (local user) for each transcript segment to determine
who was speaking. No ML dependencies — just signal-level comparison
leveraging the dual-source recording architecture.

Requires separate source WAV files from audio capture
(audio.keep_source_files must be true, set automatically when
diarisation is enabled).
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from src.transcriber import Transcript

logger = logging.getLogger(__name__)


@dataclass
class DiarisationConfig:
    enabled: bool = False
    speaker_name: str = "Me"           # Label for the local user.
    remote_label: str = "Remote"       # Label for remote participants.
    energy_ratio_threshold: float = 1.5  # How much louder one source must be.


class Diariser:
    """
    Labels transcript segments with speaker identifiers by comparing
    energy levels between mic and system audio recordings.
    """

    def __init__(self, config: DiarisationConfig):
        self._config = config

    def diarise(
        self,
        transcript: Transcript,
        system_audio_path: Path,
        mic_audio_path: Path,
    ) -> Transcript:
        """
        For each segment in the transcript, determine the speaker by
        comparing RMS energy in the corresponding time window of each
        source file.

        Modifies the transcript segments in place and returns the
        same Transcript object.
        """
        system_audio, system_sr = sf.read(
            str(system_audio_path), dtype="float32"
        )
        mic_audio, mic_sr = sf.read(str(mic_audio_path), dtype="float32")

        sample_rate = system_sr
        threshold = self._config.energy_ratio_threshold
        me = self._config.speaker_name
        remote = self._config.remote_label

        for segment in transcript.segments:
            start_sample = int(segment.start * sample_rate)
            end_sample = int(segment.end * sample_rate)

            sys_window = system_audio[
                start_sample : min(end_sample, len(system_audio))
            ]
            mic_window = mic_audio[
                start_sample : min(end_sample, len(mic_audio))
            ]

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
        logger.info(f"Diarisation complete: {counts}")

        return transcript

    @staticmethod
    def _rms(audio: np.ndarray) -> float:
        """Calculate RMS of an audio array."""
        if len(audio) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio ** 2)))
