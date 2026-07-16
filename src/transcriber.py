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
import re
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import mlx_whisper

from src.utils.config import TranscriptionConfig

logger = logging.getLogger(__name__)

# Canonical short phrases Whisper hallucinates over silence. Only suppressed
# when the segment is short AND the model reports a high no-speech probability
# (see Transcriber.transcribe), so a genuine mid-meeting "thank you" survives.
KNOWN_HALLUCINATION_PHRASES = frozenset(
    {
        "thank you",
        "thank you very much",
        "thank you for watching",
        "thanks for watching",
        "please subscribe",
        "you",
        "bye",
        "bye bye",
    }
)


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
    # Segments that the hallucination filters dropped, preserved so the
    # caller can surface "X segments filtered" to the user instead of
    # silently throwing them away (Bug B1).
    dropped_segments: list[TranscriptSegment] = field(default_factory=list)

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
            "dropped_segments": [asdict(s) for s in self.dropped_segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        """Deserialise from the dict shape produced by `to_dict` (symmetric)."""
        data = data or {}
        segs = [
            TranscriptSegment(
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                text=s.get("text", ""),
                speaker=s.get("speaker", ""),
            )
            for s in data.get("segments", [])
        ]
        return cls(
            segments=segs,
            language=data.get("language", ""),
            language_probability=data.get("language_probability", 0.0),
            duration_seconds=data.get("duration_seconds", 0.0),
        )


class Transcriber:
    """
    Wraps MLX Whisper to provide file-level transcription.

    MLX Whisper runs on Apple Silicon GPU automatically.
    The model is downloaded and cached on first use.
    """

    def __init__(self, config: TranscriptionConfig):
        self._config = config

    @staticmethod
    def _is_repetition_hallucination(text: str, max_consecutive: int = 5) -> bool:
        """Detect repeated-word hallucinations (e.g. 'Dios Dios Dios ...')."""
        words = text.lower().split()
        if len(words) < max_consecutive:
            return False
        count = 1
        for i in range(1, len(words)):
            if words[i] == words[i - 1]:
                count += 1
                if count >= max_consecutive:
                    return True
            else:
                count = 1
        return False

    @staticmethod
    def _text_compression_ratio(text: str) -> float:
        """Ratio of total length to unique-character count (high = repetitive)."""
        if not text:
            return 0.0
        unique = len(set(text.lower()))
        if unique == 0:
            return 0.0
        return len(text) / unique

    @staticmethod
    def _is_phrase_repetition(text: str, min_repeats: int = 3) -> bool:
        """Detect a short phrase repeated back-to-back (e.g. 'thank you.
        thank you. thank you.') that the single-word filter misses."""
        parts = [p.strip().lower() for p in re.split(r"[.!?]+", text) if p.strip()]
        if len(parts) >= min_repeats and len(set(parts)) == 1:
            return True
        # Also catch whitespace-separated n-gram repeats without punctuation.
        words = text.lower().split()
        for n in (1, 2, 3):
            if len(words) < n * min_repeats:
                continue
            grams = [tuple(words[i : i + n]) for i in range(0, len(words) - n + 1, n)]
            run = 1
            for i in range(1, len(grams)):
                run = run + 1 if grams[i] == grams[i - 1] else 1
                if run >= min_repeats:
                    return True
        return False

    @staticmethod
    def _is_known_hallucination_phrase(text: str) -> bool:
        """True if the whole segment is a canonical silence hallucination."""
        normalised = text.strip().lower().strip(".,!?").strip()
        return normalised in KNOWN_HALLUCINATION_PHRASES

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
            condition_on_previous_text=self._config.condition_on_previous_text,
            compression_ratio_threshold=self._config.compression_ratio_threshold,
            logprob_threshold=self._config.logprob_threshold,
            no_speech_threshold=self._config.no_speech_threshold,
            hallucination_silence_threshold=self._config.hallucination_silence_threshold,
            temperature=tuple(self._config.temperature),
            initial_prompt=self._config.initial_prompt or None,
            verbose=False,
        )

        segments: list[TranscriptSegment] = []
        dropped: list[TranscriptSegment] = []
        last_end = -1.0
        for seg_dict in result.get("segments", []):
            text = seg_dict["text"].strip()
            if not text:
                continue

            start = seg_dict["start"]
            end = seg_dict["end"]
            no_speech = seg_dict.get("no_speech_prob", 0.0)
            ts = TranscriptSegment(start=start, end=end, text=text)

            # Timestamp monotonicity: skip segments that jump backwards.
            if start < last_end - 0.1:
                logger.warning(
                    "Skipping backward segment [%.1f-%.1f]: %s",
                    start,
                    end,
                    text[:80],
                )
                dropped.append(ts)
                continue

            # Repetition hallucination filter.
            if self._is_repetition_hallucination(text):
                logger.warning(
                    "Skipping repetition hallucination [%.1f-%.1f]: %s",
                    start,
                    end,
                    text[:80],
                )
                dropped.append(ts)
                continue

            # High compression ratio filter (very repetitive character patterns).
            if self._text_compression_ratio(text) > 15.0 and len(text) > 20:
                logger.warning(
                    "Skipping high-compression segment [%.1f-%.1f]: %s",
                    start,
                    end,
                    text[:80],
                )
                dropped.append(ts)
                continue

            # Phrase-level repetition (e.g. "thank you. thank you. thank you.").
            if self._is_phrase_repetition(text, self._config.phrase_repetition_min_repeats):
                logger.warning(
                    "Skipping phrase-repetition hallucination [%.1f-%.1f]: %s",
                    start,
                    end,
                    text[:80],
                )
                dropped.append(ts)
                continue

            # Known filler phrase emitted during silence.
            if (
                len(text.split()) <= self._config.hallucination_phrase_max_words
                and self._is_known_hallucination_phrase(text)
                and no_speech >= self._config.hallucination_phrase_no_speech_threshold
            ):
                logger.warning(
                    "Skipping silence hallucination [%.1f-%.1f]: %s",
                    start,
                    end,
                    text[:80],
                )
                dropped.append(ts)
                continue

            segments.append(ts)
            last_end = end
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
            dropped_segments=dropped,
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
