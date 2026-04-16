"""
Speech-to-text transcription using MLX Whisper.

Accepts a WAV file path and returns a structured transcript with
timestamps. MLX Whisper runs on Apple Silicon GPU via the MLX
framework, providing ~10x faster transcription compared to
CPU-based engines.

Model download happens automatically on first use. Models are cached
in ~/.cache/huggingface/hub/ by default.
"""

import logging
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import mlx_whisper

from src.utils.config import TranscriptionConfig

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    """A single timed segment of the transcript."""

    start: float  # Start time in seconds.
    end: float  # End time in seconds.
    text: str  # Transcribed text for this segment.
    speaker: str = ""  # Speaker label (future: diarisation).

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
                lines.append(f"{seg.timestamp} [{seg.speaker}] {seg.text.strip()}")
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
    Wraps MLX Whisper to provide file-level transcription.

    MLX Whisper runs on Apple Silicon GPU automatically.
    The model is downloaded and cached on first use.
    """

    def __init__(self, config: TranscriptionConfig):
        self._config = config

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
        by AudioCapture). MLX Whisper handles resampling internally
        if the format differs, but 16kHz is optimal.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        logger.info("Transcribing %s...", audio_path)
        start_time = _time.monotonic()

        language = None if self._config.language == "auto" else self._config.language

        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self._config.model_size,
            language=language,
        )

        segments = []
        for seg_dict in result.get("segments", []):
            ts = TranscriptSegment(
                start=seg_dict["start"],
                end=seg_dict["end"],
                text=seg_dict["text"],
            )
            segments.append(ts)
            if on_segment:
                try:
                    on_segment(ts)
                except Exception:
                    logger.debug("on_segment callback failed", exc_info=True)

        # Calculate duration from last segment end time.
        duration = segments[-1].end if segments else 0.0

        elapsed = _time.monotonic() - start_time
        transcript = Transcript(
            segments=segments,
            language=result.get("language", ""),
            language_probability=0.0,  # MLX Whisper doesn't provide this.
            duration_seconds=duration,
        )

        rtf = elapsed / duration if duration > 0 else 0
        logger.info(
            "Transcription complete: %d words, %d segments, %.1fs elapsed (%.1fx realtime).",
            transcript.word_count,
            len(segments),
            elapsed,
            rtf,
        )

        return transcript
