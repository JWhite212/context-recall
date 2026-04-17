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

from src.diariser import DiarisationConfig
from src.transcriber import Transcript

logger = logging.getLogger(__name__)


class PyAnnoteDiariser:
    """
    Labels transcript segments with speaker identifiers using pyannote.audio.

    Implements the DiariserBackend Protocol
    (diarise(transcript, audio_path) -> Transcript).
    """

    def __init__(self, config: DiarisationConfig) -> None:
        self._config = config
        self._pipeline = None  # Lazy-loaded
        self._lock = threading.Lock()

    def _load_pipeline(self) -> None:
        """Lazy-load the pyannote pipeline."""
        from pyannote.audio import Pipeline

        if not os.environ.get("HF_TOKEN"):
            logger.warning("HF_TOKEN not set — pyannote model download may fail for gated models")
        self._pipeline = Pipeline.from_pretrained(
            self._config.pyannote_model,
            use_auth_token=os.environ.get("HF_TOKEN"),
        )
        logger.info("Loaded pyannote pipeline: %s", self._config.pyannote_model)

    def diarise(self, transcript: Transcript, audio_path: Path) -> Transcript:
        """
        Label each segment in *transcript* with a speaker identifier.

        Runs the pyannote pipeline on the combined audio file, then
        aligns the resulting speaker turns with the transcript segments
        by temporal overlap.
        """
        if self._pipeline is None:
            with self._lock:
                if self._pipeline is None:
                    self._load_pipeline()

        # Run pyannote diarisation.
        params: dict = {}
        if self._config.num_speakers > 0:
            params["num_speakers"] = self._config.num_speakers

        diarisation = self._pipeline(str(audio_path), **params)

        # Build a list of (start, end, speaker_label) turns.
        turns: list[tuple[float, float, str]] = []
        for turn, _, speaker in diarisation.itertracks(yield_label=True):
            turns.append((turn.start, turn.end, speaker))

        # Assign speakers to transcript segments by maximum temporal overlap.
        for segment in transcript.segments:
            best_speaker = ""
            best_overlap = 0.0

            for turn_start, turn_end, speaker in turns:
                overlap_start = max(segment.start, turn_start)
                overlap_end = min(segment.end, turn_end)
                overlap = max(0.0, overlap_end - overlap_start)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = speaker

            segment.speaker = best_speaker

        # Log summary.
        counts: dict[str, int] = {}
        for seg in transcript.segments:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
        logger.info("Pyannote diarisation complete: %s", counts)

        return transcript
