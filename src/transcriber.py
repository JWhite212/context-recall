"""
Speech-to-text transcription using faster-whisper.

Accepts a WAV file path and returns a structured transcript with
timestamps. faster-whisper is a CTranslate2-backed reimplementation
of OpenAI Whisper that runs significantly faster on CPU, making it
practical for real-time-ish transcription on Apple Silicon.

Model download happens automatically on first use. Models are cached
in ~/.cache/huggingface/hub/ by default.
"""

import logging
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from faster_whisper import WhisperModel

from src.utils.config import TranscriptionConfig

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single timed segment of the transcript."""

    start: float           # Start time in seconds.
    end: float             # End time in seconds.
    text: str              # Transcribed text for this segment.
    speaker: str = ""      # Speaker label (future: diarisation).

    @property
    def timestamp(self) -> str:
        """Format start time as [HH:MM:SS] for display."""
        h, remainder = divmod(int(self.start), 3600)
        m, s = divmod(remainder, 60)
        return f"[{h:02d}:{m:02d}:{s:02d}]"


@dataclass
class Transcript:
    """Complete transcript of a meeting."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    duration_seconds: float = 0.0

    @property
    def full_text(self) -> str:
        """Concatenated plain text of all segments."""
        return " ".join(seg.text.strip() for seg in self.segments)

    @property
    def timestamped_text(self) -> str:
        """Formatted transcript with timestamps and optional speaker labels."""
        lines = []
        for seg in self.segments:
            if seg.speaker:
                lines.append(
                    f"{seg.timestamp} [{seg.speaker}] {seg.text.strip()}"
                )
            else:
                lines.append(f"{seg.timestamp} {seg.text.strip()}")
        return "\n".join(lines)

    @property
    def word_count(self) -> int:
        return sum(len(seg.text.split()) for seg in self.segments)

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict for database storage."""
        return {
            "segments": [asdict(s) for s in self.segments],
            "language": self.language,
            "language_probability": self.language_probability,
            "duration_seconds": self.duration_seconds,
        }


class Transcriber:
    """
    Wraps faster-whisper to provide file-level transcription.

    The model is loaded lazily on first call to transcribe(), so
    constructing a Transcriber instance is cheap.
    """

    def __init__(self, config: TranscriptionConfig):
        self._config = config
        self._model: WhisperModel | None = None

    def _load_model(self) -> WhisperModel:
        """Load the Whisper model on first use."""
        if self._model is not None:
            return self._model

        logger.info(
            "Loading faster-whisper model '%s' (compute_type=%s)...",
            self._config.model_size,
            self._config.compute_type,
        )

        # Determine compute type for Apple Silicon.
        compute_type = self._config.compute_type
        if compute_type == "auto":
            # int8 is the fastest option on CPU and works well on Apple Silicon.
            compute_type = "int8"

        cpu_threads = self._config.cpu_threads or 0  # 0 = auto-detect.

        self._model = WhisperModel(
            self._config.model_size,
            device="cpu",          # faster-whisper doesn't support MPS yet.
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )

        logger.info("Model loaded successfully.")
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        on_segment: Callable[[TranscriptSegment], None] | None = None,
    ) -> Transcript:
        """
        Transcribe a WAV file and return a structured Transcript.

        If *on_segment* is provided, it is called with each segment as it
        is produced, enabling real-time streaming to the UI.

        The audio file should be 16kHz mono PCM (the format produced
        by AudioCapture). faster-whisper handles resampling internally
        if the format differs, but 16kHz is optimal.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        model = self._load_model()

        logger.info("Transcribing %s...", audio_path)
        start_time = _time.monotonic()

        segments_iter, info = model.transcribe(
            str(audio_path),
            language=self._config.language if self._config.language != "auto" else None,
            beam_size=5,
            vad_filter=True,           # Filter out silence for speed.
            vad_parameters=dict(
                min_silence_duration_ms=500,
                threshold=self._config.vad_threshold,
            ),
        )

        # Materialise the generator into a list of TranscriptSegments.
        segments = []
        for seg in segments_iter:
            ts = TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
            )
            segments.append(ts)
            if on_segment:
                try:
                    on_segment(ts)
                except Exception:
                    logger.warning(
                        "on_segment callback failed for segment at %.1fs",
                        ts.start,
                        exc_info=True,
                    )

        elapsed = _time.monotonic() - start_time
        transcript = Transcript(
            segments=segments,
            language=info.language,
            language_probability=info.language_probability,
            duration_seconds=info.duration,
        )

        logger.info(
            "Transcription complete: %d words, %d segments, "
            "%.1fs elapsed (%.1fx realtime).",
            transcript.word_count,
            len(segments),
            elapsed,
            info.duration / elapsed if elapsed > 0 else 0,
        )

        return transcript
