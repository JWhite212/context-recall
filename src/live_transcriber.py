"""
Live transcription during recording via chunked MLX Whisper.

Accumulates audio samples in a thread-safe buffer and periodically
transcribes them using the same MLX Whisper engine as batch mode.
Emits TranscriptSegments via callback as new text is produced.

The live transcript is for real-time UI display only — the final
stored transcript comes from a full batch run after recording stops,
which has higher accuracy due to full-context processing.
"""

import concurrent.futures
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from src.transcriber import TranscriptSegment

logger = logging.getLogger("contextrecall.live")

# Window over which dropped-chunk counts accumulate before being flushed as
# a single pipeline.warning. Avoids spamming the UI when the audio thread
# bursts faster than the transcriber can drain.
_DROP_WARN_WINDOW_SECONDS = 5.0

# Hard cap on a single MLX transcribe call. Bad input has been observed to
# wedge the kernel indefinitely; without a bound, LiveTranscriber.stop()
# would hang on the worker join.
_MLX_TRANSCRIBE_TIMEOUT_SECONDS = 60.0


@dataclass
class LiveTranscriptionConfig:
    chunk_interval_seconds: float = 8.0
    min_chunk_seconds: float = 2.0
    overlap_seconds: float = 2.0
    silence_rms_threshold: float = 0.01


class LiveTranscriber:
    """Chunked live transcription using MLX Whisper.

    Audio chunks are fed via feed() from the PortAudio callback thread
    (must be non-blocking). A worker thread periodically concatenates
    buffered audio and runs transcription, emitting new segments.
    """

    def __init__(
        self,
        model_size: str,
        language: str = "en",
        on_segment=None,
        sample_rate: int = 16000,
        config: LiveTranscriptionConfig | None = None,
        on_warning: Callable[[dict], None] | None = None,
    ):
        self._model_size = model_size
        self._language = None if language == "auto" else language
        self._on_segment = on_segment
        self._sample_rate = sample_rate
        self._config = config or LiveTranscriptionConfig()
        self.on_warning: Callable[[dict], None] | None = on_warning

        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=500)
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_text = ""
        self._total_offset_samples = 0

        # Dropped-chunk counter + monotonic window anchor. The audio
        # callback thread bumps these; the worker (or stop()) flushes.
        self._dropped_chunks = 0
        self._dropped_window_started_at: float | None = None
        self._drop_lock = threading.Lock()

    def feed(self, audio_chunk: np.ndarray) -> None:
        """Feed audio samples. Thread-safe, non-blocking. Called from PortAudio callback."""
        try:
            self._audio_queue.put_nowait(audio_chunk.copy())
        except queue.Full:
            # Drop rather than block the audio thread; record the drop so
            # _maybe_flush_drop_warning can surface it as a pipeline.warning.
            now = time.monotonic()
            with self._drop_lock:
                self._dropped_chunks += 1
                if self._dropped_window_started_at is None:
                    self._dropped_window_started_at = now

    def start(self) -> None:
        """Start the transcription worker thread."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._previous_text = ""
        self._total_offset_samples = 0
        self._thread = threading.Thread(
            target=self._worker_loop, name="live-transcriber", daemon=True
        )
        self._thread.start()
        logger.info(
            "Live transcriber started (interval=%.1fs)", self._config.chunk_interval_seconds
        )

    def stop(self) -> None:
        """Stop the worker thread and process remaining buffer."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()  # Interrupt sleep immediately.
        if self._thread:
            self._thread.join(timeout=30)
            self._thread = None
        # Flush any final drop-counter activity so a burst right at
        # shutdown isn't silently lost.
        self._maybe_flush_drop_warning(force=True)
        logger.info("Live transcriber stopped")

    def _worker_loop(self) -> None:
        """Main worker: sleep, drain queue, transcribe, emit new segments."""
        buffer = np.array([], dtype=np.float32)
        overlap_samples = int(self._config.overlap_seconds * self._sample_rate)

        while self._running:
            self._stop_event.wait(timeout=self._config.chunk_interval_seconds)
            if not self._running:
                break
            buffer = self._drain_queue(buffer)
            self._maybe_flush_drop_warning()

            duration = len(buffer) / self._sample_rate
            if duration < self._config.min_chunk_seconds:
                continue

            # Skip if audio is silence.
            rms = float(np.sqrt(np.mean(buffer**2)))
            if rms < self._config.silence_rms_threshold:
                continue

            self._transcribe_chunk(buffer)

            # Keep overlap for next chunk to avoid word boundary splits.
            if len(buffer) > overlap_samples:
                self._total_offset_samples += len(buffer) - overlap_samples
                buffer = buffer[-overlap_samples:]
            else:
                self._total_offset_samples += len(buffer)
                buffer = np.array([], dtype=np.float32)

        # Process any remaining buffer on shutdown.
        buffer = self._drain_queue(buffer)
        if len(buffer) / self._sample_rate >= self._config.min_chunk_seconds:
            self._transcribe_chunk(buffer)

    def _maybe_flush_drop_warning(self, force: bool = False) -> None:
        """If the drop window has elapsed (or force=True), emit a warning.

        Resets the counter and window anchor afterwards. Safe to call from
        the worker thread; takes the lock briefly to read+reset the shared
        counter incremented by feed().
        """
        with self._drop_lock:
            count = self._dropped_chunks
            window_start = self._dropped_window_started_at
            if count == 0 or window_start is None:
                return
            elapsed = time.monotonic() - window_start
            if not force and elapsed < _DROP_WARN_WINDOW_SECONDS:
                return
            # Snapshot + reset under the lock so concurrent feed() drops
            # always belong to either this window or the next, never both.
            self._dropped_chunks = 0
            self._dropped_window_started_at = None

        logger.warning(
            "Live transcriber dropped %d audio chunk(s) in %.1fs — "
            "transcribe is falling behind audio capture.",
            count,
            elapsed,
        )
        callback = self.on_warning
        if callback is None:
            return
        try:
            callback(
                {
                    "type": "live_chunk_drop",
                    "count": count,
                    "window_seconds": round(elapsed, 2),
                }
            )
        except Exception:
            # Never let an event-bus failure break the live transcriber.
            logger.exception("on_warning callback raised; ignoring")

    def _drain_queue(self, buffer: np.ndarray) -> np.ndarray:
        """Drain all pending audio chunks from the queue into the buffer."""
        chunks = [buffer] if len(buffer) > 0 else []
        while True:
            try:
                chunk = self._audio_queue.get_nowait()
                chunks.append(chunk)
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.float32)
        return np.concatenate(chunks)

    def _transcribe_chunk(self, audio: np.ndarray) -> None:
        """Transcribe an audio buffer and emit new segments.

        The MLX call is dispatched to a single-shot ThreadPoolExecutor with
        a hard deadline so a wedged kernel can't block this worker — which
        would in turn block stop()'s join and stall meeting teardown. On
        timeout the executor is shut down with wait=False; the orphan
        thread is unavoidable (Python has no thread kill) but unblocking
        the pipeline matters more.
        """
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="live-mlx"
        )
        try:
            import mlx_whisper

            future = executor.submit(
                mlx_whisper.transcribe,
                audio,
                path_or_hf_repo=self._model_size,
                language=self._language,
                word_timestamps=False,
            )
            try:
                result = future.result(timeout=_MLX_TRANSCRIBE_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                future.cancel()
                logger.warning(
                    "Live transcription chunk timed out after %.0fs — skipping",
                    _MLX_TRANSCRIBE_TIMEOUT_SECONDS,
                )
                return
        except Exception as e:
            logger.warning("Live transcription chunk failed: %s", e)
            return
        finally:
            executor.shutdown(wait=False)

        segments = result.get("segments", [])
        if not segments:
            return

        # Build full text from this chunk for deduplication.
        chunk_text = " ".join(s.get("text", "").strip() for s in segments)

        # Find new text by comparing with previous output.
        new_text = self._find_new_text(chunk_text)
        if not new_text:
            return

        self._previous_text = chunk_text

        # Emit new segments with adjusted timestamps.
        time_offset = self._total_offset_samples / self._sample_rate
        for seg in segments:
            seg_text = seg.get("text", "").strip()
            if not seg_text:
                continue
            # Only emit segments whose text appears in the new portion.
            if seg_text in new_text:
                segment = TranscriptSegment(
                    start=round(seg.get("start", 0.0) + time_offset, 2),
                    end=round(seg.get("end", 0.0) + time_offset, 2),
                    text=seg_text,
                    speaker="",
                )
                if self._on_segment:
                    try:
                        self._on_segment(segment)
                    except Exception:
                        pass

    def _find_new_text(self, current_text: str) -> str:
        """Find text in current output that wasn't in the previous output."""
        if not self._previous_text:
            return current_text

        # Simple suffix-based deduplication: find the longest overlap
        # between the end of previous text and the start of current text.
        prev_words = self._previous_text.split()
        curr_words = current_text.split()

        if not prev_words or not curr_words:
            return current_text

        # Try progressively shorter suffixes of previous text.
        max_overlap = min(len(prev_words), len(curr_words))
        for overlap_len in range(max_overlap, 0, -1):
            if prev_words[-overlap_len:] == curr_words[:overlap_len]:
                new_words = curr_words[overlap_len:]
                return " ".join(new_words) if new_words else ""

        return current_text
