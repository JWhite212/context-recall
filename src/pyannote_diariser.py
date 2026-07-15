"""
PyAnnote-based speaker diarisation.

Uses pyannote.audio's pretrained speaker diarisation pipeline to
identify individual speakers in a meeting recording. Requires a
HuggingFace token with access to the pyannote models.

This is an optional backend -- the module guards its imports so it
can be loaded even without torch/pyannote installed. The heavy
``from pyannote.audio import Pipeline`` import is deferred to
``_load_pipeline()`` so construction is cheap.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class PyAnnoteDiariser:
    """Hybrid diariser: energy decides me-vs-remote, pyannote separates the
    remote speakers.

    The mic channel is unambiguously the local user, so this runs the energy
    diariser first (mic-vs-system RMS) to mark the user's segments as
    ``speaker_name``, then runs pyannote over the *remote* (system) source
    WAV and overlays ``SPEAKER_NN`` onto every non-user segment. This keeps
    "Me" reliable while separating multiple remote participants.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._pipeline = None  # Lazy-loaded
        self._lock = threading.Lock()

    def _load_pipeline(self) -> None:
        """Lazy-load the pyannote pipeline (the model is gated — needs HF_TOKEN)."""
        from pyannote.audio import Pipeline

        if not os.environ.get("HF_TOKEN"):
            logger.warning(
                "HF_TOKEN not set — the gated pyannote model may fail to load; "
                "diarisation will degrade to the energy backend."
            )
        self._pipeline = Pipeline.from_pretrained(
            self._config.pyannote_model,
            use_auth_token=os.environ.get("HF_TOKEN"),
        )
        logger.info("Loaded pyannote pipeline: %s", self._config.pyannote_model)

    def _speaker_turns(self, audio_path: Path) -> list[tuple[float, float, str]]:
        """Run pyannote on *audio_path* and return (start, end, label) turns."""
        if self._pipeline is None:
            with self._lock:
                if self._pipeline is None:
                    self._load_pipeline()
        params: dict = {}
        if self._config.num_speakers > 0:
            params["num_speakers"] = self._config.num_speakers
        diarisation = self._pipeline(str(audio_path), **params)
        return [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in diarisation.itertracks(yield_label=True)
        ]

    def diarise(
        self,
        transcript,
        audio_path: Path,
        *,
        mic_audio_path: Path | None = None,
        system_audio_path: Path | None = None,
    ):
        """Label each segment: local user → ``speaker_name``; remote → SPEAKER_NN."""
        from src.diariser import EnergyDiariser

        me = self._config.speaker_name

        # The remote (system) channel — used by both the energy pre-pass and
        # pyannote. It must be the SYSTEM-only source, never the merged
        # positional audio_path (which embeds an amplified copy of the mic and
        # would stop the user's own segments from ever being marked "Me"). Fall
        # back to the merged file only when the system source is already gone.
        remote_wav = (
            system_audio_path
            if system_audio_path is not None and Path(system_audio_path).exists()
            else audio_path
        )

        # Step 1: energy me/remote (only when the mic source survives).
        if mic_audio_path is not None and Path(mic_audio_path).exists():
            try:
                EnergyDiariser(self._config).diarise(
                    transcript, remote_wav, mic_audio_path=mic_audio_path
                )
            except Exception as e:
                logger.warning("Energy pre-pass failed (%s); treating all as remote", e)
                for seg in transcript.segments:
                    seg.speaker = ""

        # Step 2: pyannote over the remote (system) channel.
        turns = self._speaker_turns(Path(remote_wav))

        # Step 3: overlay SPEAKER_NN onto every non-user segment.
        for segment in transcript.segments:
            if segment.speaker == me:
                continue
            best_speaker, best_overlap = "", 0.0
            for turn_start, turn_end, speaker in turns:
                overlap = max(0.0, min(segment.end, turn_end) - max(segment.start, turn_start))
                if overlap > best_overlap:
                    best_overlap, best_speaker = overlap, speaker
            segment.speaker = best_speaker or self._config.remote_label

        counts: dict[str, int] = {}
        for seg in transcript.segments:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
        logger.info("Hybrid pyannote diarisation complete: %s", counts)
        return transcript
