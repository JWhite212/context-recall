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
from dataclasses import dataclass, field
from pathlib import Path

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
        """Formatted transcript with timestamps for each segment."""
        lines = []
        for seg in self.segments:
            lines.append(f"{seg.timestamp} {seg.text.strip()}")
        return "\n".join(lines)

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())


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
            f"Loading faster-whisper model '{self._config.model_size}' "
            f"(compute_type={self._config.compute_type})..."
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

    def transcribe(self, audio_path: Path) -> Transcript:
        """
        Transcribe a WAV file and return a structured Transcript.

        The audio file should be 16kHz mono PCM (the format produced
        by AudioCapture). faster-whisper handles resampling internally
        if the format differs, but 16kHz is optimal.
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        model = self._load_model()

        logger.info(f"Transcribing {audio_path}...")
        start_time = __import__("time").time()

        segments_iter, info = model.transcribe(
            str(audio_path),
            language=self._config.language if self._config.language != "auto" else None,
            beam_size=5,
            vad_filter=True,           # Filter out silence for speed.
            vad_parameters=dict(
                min_silence_duration_ms=500,
            ),
        )

        # Materialise the generator into a list of TranscriptSegments.
        segments = []
        for seg in segments_iter:
            segments.append(
                TranscriptSegment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                )
            )

        elapsed = __import__("time").time() - start_time
        transcript = Transcript(
            segments=segments,
            language=info.language,
            language_probability=info.language_probability,
            duration_seconds=info.duration,
        )

        logger.info(
            f"Transcription complete: {transcript.word_count} words, "
            f"{len(segments)} segments, {elapsed:.1f}s elapsed "
            f"({info.duration / elapsed:.1f}x realtime)."
        )

        return transcript
