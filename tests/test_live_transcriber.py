"""Tests for src/live_transcriber.py — backpressure + MLX timeout observability.

These cover:
  - feed() drops chunks under load and increments a counter instead of
    silently swallowing them.
  - The drop counter is flushed via on_warning as a structured
    pipeline.warning payload (type=live_chunk_drop, count=...) after the
    drop window elapses.
  - _transcribe_chunk bounds the MLX call with a 60s timeout so a hung
    kernel can't block stop().
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import numpy as np
import pytest

from src.live_transcriber import (
    _DROP_WARN_WINDOW_SECONDS,
    _MLX_TRANSCRIBE_TIMEOUT_SECONDS,
    LiveTranscriber,
    LiveTranscriptionConfig,
)


def _make_lt(**kwargs) -> LiveTranscriber:
    """Build a LiveTranscriber with a tiny queue so we can trip backpressure."""
    return LiveTranscriber(
        model_size="tiny.en",
        language="en",
        sample_rate=16000,
        config=LiveTranscriptionConfig(chunk_interval_seconds=10.0),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Backpressure: feed() must record drops, not silently discard them.
# ---------------------------------------------------------------------------


def test_feed_counts_dropped_chunks_when_queue_full():
    """When the audio queue is saturated, feed() must increment the
    _dropped_chunks counter rather than swallowing the chunk silently."""
    lt = _make_lt()
    # Force the queue full so put_nowait always raises.
    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))

    chunk = np.zeros(16, dtype=np.float32)
    for _ in range(7):
        lt.feed(chunk)

    assert lt._dropped_chunks == 7
    assert lt._dropped_window_started_at is not None


def test_feed_does_not_block_or_raise_when_full():
    """feed() runs on the PortAudio callback thread — it must never block
    or surface an exception even when the queue is jammed."""
    lt = _make_lt()
    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))

    chunk = np.zeros(16, dtype=np.float32)
    t0 = time.monotonic()
    for _ in range(50):
        lt.feed(chunk)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, "feed() must remain near-instant even on saturation"


# ---------------------------------------------------------------------------
# on_warning callback: structured pipeline.warning payload.
# ---------------------------------------------------------------------------


def test_drop_warning_fires_after_window_elapses():
    """After at least _DROP_WARN_WINDOW_SECONDS of dropped-chunk activity,
    on_warning must be invoked exactly once with the structured payload
    the orchestrator forwards as pipeline.warning."""
    warnings: list[dict] = []
    lt = _make_lt(on_warning=warnings.append)

    # Saturate the queue then drop a few chunks.
    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))
    for _ in range(3):
        lt.feed(np.zeros(16, dtype=np.float32))

    # Spoof the window-start anchor to simulate elapsed time without sleeping
    # for the full 5 seconds.
    with lt._drop_lock:
        lt._dropped_window_started_at = time.monotonic() - (_DROP_WARN_WINDOW_SECONDS + 0.1)

    lt._maybe_flush_drop_warning()

    assert len(warnings) == 1
    payload = warnings[0]
    assert payload["type"] == "live_chunk_drop"
    assert payload["count"] == 3
    assert payload["window_seconds"] >= _DROP_WARN_WINDOW_SECONDS
    # Counter must be reset so the next window starts clean.
    assert lt._dropped_chunks == 0
    assert lt._dropped_window_started_at is None


def test_drop_warning_does_not_fire_inside_window():
    """If the drop window hasn't elapsed yet, no warning should fire — we
    don't want to spam the UI for every burst."""
    warnings: list[dict] = []
    lt = _make_lt(on_warning=warnings.append)

    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))
    lt.feed(np.zeros(16, dtype=np.float32))

    lt._maybe_flush_drop_warning()

    assert warnings == []
    assert lt._dropped_chunks == 1, "counter must persist until window elapses"


def test_drop_warning_force_flushes_on_stop():
    """stop() flushes the drop counter unconditionally so a burst right at
    teardown isn't silently lost."""
    warnings: list[dict] = []
    lt = _make_lt(on_warning=warnings.append)

    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))
    lt.feed(np.zeros(16, dtype=np.float32))

    lt._maybe_flush_drop_warning(force=True)

    assert len(warnings) == 1
    assert warnings[0]["type"] == "live_chunk_drop"
    assert warnings[0]["count"] == 1


def test_drop_warning_callback_failure_is_swallowed():
    """A buggy on_warning callback must not crash the transcriber."""

    def boom(_payload: dict) -> None:
        raise RuntimeError("downstream bus is down")

    lt = _make_lt(on_warning=boom)

    for _ in range(lt._audio_queue.maxsize):
        lt._audio_queue.put_nowait(np.zeros(16, dtype=np.float32))
    lt.feed(np.zeros(16, dtype=np.float32))

    with lt._drop_lock:
        lt._dropped_window_started_at = time.monotonic() - (_DROP_WARN_WINDOW_SECONDS + 0.1)

    # Must not propagate.
    lt._maybe_flush_drop_warning()


# ---------------------------------------------------------------------------
# _transcribe_chunk: MLX call must be bounded so stop() can't hang.
# ---------------------------------------------------------------------------


def test_transcribe_chunk_times_out_when_mlx_hangs():
    """If mlx_whisper.transcribe wedges, _transcribe_chunk must return
    within the configured timeout rather than blocking forever. We monkey-
    patch the import target with a callable that blocks until released and
    use a very short timeout patched in at module level so the test runs
    in seconds, not minutes."""
    release = threading.Event()
    started = threading.Event()

    def _hanging_transcribe(*_args, **_kwargs):
        started.set()
        # Block until the test releases us — guarded so we always return
        # eventually if the test finishes.
        release.wait(timeout=10.0)
        return {"segments": []}

    fake_mlx = type("FakeMLX", (), {"transcribe": staticmethod(_hanging_transcribe)})

    lt = _make_lt()
    audio = np.zeros(16000, dtype=np.float32)

    with patch.dict("sys.modules", {"mlx_whisper": fake_mlx}):
        with patch("src.live_transcriber._MLX_TRANSCRIBE_TIMEOUT_SECONDS", 0.5):
            t0 = time.monotonic()
            lt._transcribe_chunk(audio)
            elapsed = time.monotonic() - t0

    release.set()  # let the orphan thread finish cleanly

    assert started.is_set(), "the transcribe call must have been attempted"
    assert elapsed < 2.0, (
        f"_transcribe_chunk blocked for {elapsed:.2f}s; the MLX call must "
        "be bounded by _MLX_TRANSCRIBE_TIMEOUT_SECONDS"
    )


def test_transcribe_chunk_returns_normally_when_mlx_succeeds():
    """The timeout wrapper must not break the happy path: a fast MLX
    response should still produce segment emissions."""
    emitted: list = []

    def _capture(seg):
        emitted.append(seg)

    fast_result = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "hello world"},
        ]
    }
    fake_mlx = type("FakeMLX", (), {"transcribe": staticmethod(lambda *a, **k: fast_result)})

    lt = _make_lt(on_segment=_capture)
    audio = np.zeros(16000, dtype=np.float32)

    with patch.dict("sys.modules", {"mlx_whisper": fake_mlx}):
        lt._transcribe_chunk(audio)

    assert len(emitted) == 1
    assert emitted[0].text == "hello world"


def test_module_constants_are_sensible_defaults():
    """Sanity-check the module-level constants — these are the contract
    the orchestrator depends on."""
    assert _DROP_WARN_WINDOW_SECONDS == 5.0
    assert _MLX_TRANSCRIBE_TIMEOUT_SECONDS == 60.0


# ---------------------------------------------------------------------------
# Orchestrator wiring: main.py must wire on_warning -> pipeline.warning.
# ---------------------------------------------------------------------------


@pytest.fixture
def main_tmp_config(tmp_path):
    """Minimal config.yaml that ContextRecall accepts, with live transcription on."""
    import yaml

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    config = {
        "detection": {"poll_interval_seconds": 1},
        "audio": {
            "sample_rate": 16000,
            "temp_audio_dir": str(tmp_path / "audio"),
        },
        "transcription": {"model_size": "tiny.en", "live_enabled": True},
        "summarisation": {"backend": "ollama"},
        "markdown": {"enabled": False},
        "notion": {"enabled": False},
        "diarisation": {"enabled": False},
        "api": {"enabled": False},
        "logging": {
            "level": "WARNING",
            "log_file": str(log_dir / "test.log"),
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(config))
    return path


@patch("src.main.Summariser")
@patch("src.main.TeamsDetector")
@patch("src.main.Transcriber")
@patch("src.main.AudioCapture")
def test_on_meeting_start_wires_live_transcriber_warning_callback(
    mock_capture_cls,
    mock_transcriber_cls,
    mock_detector_cls,
    mock_summariser_cls,
    main_tmp_config,
):
    """When a meeting starts and live transcription is enabled, the
    orchestrator must wire LiveTranscriber.on_warning so its structured
    payloads land on the event bus as pipeline.warning events."""
    from src.detector import MeetingEvent, MeetingState
    from src.main import ContextRecall

    app = ContextRecall(config_path=main_tmp_config)

    # Spy on emit so we can verify the warning callback forwards correctly.
    emitted: list[tuple[str, dict]] = []
    app._emit = lambda event_type, **kwargs: emitted.append((event_type, kwargs))

    # Patch LiveTranscriber so we can inspect the on_warning argument that
    # main.py passes in (without spinning up MLX).
    with patch("src.live_transcriber.LiveTranscriber") as mock_lt_cls:
        mock_lt = mock_lt_cls.return_value
        mock_lt.feed = lambda *_a, **_k: None
        mock_lt.start = lambda: None

        event = MeetingEvent(
            state=MeetingState.ACTIVE,
            started_at=time.time(),
        )
        app._on_meeting_start(event)

    # main.py must have passed an on_warning callable into the constructor.
    assert mock_lt_cls.called
    kwargs = mock_lt_cls.call_args.kwargs
    assert "on_warning" in kwargs, (
        "main.py must pass on_warning to LiveTranscriber so drop events "
        "are surfaced as pipeline.warning"
    )
    on_warning = kwargs["on_warning"]
    assert callable(on_warning)

    # Simulate the live transcriber raising a drop warning — main.py must
    # convert it into a pipeline.warning event with the structured payload.
    on_warning({"type": "live_chunk_drop", "count": 4, "window_seconds": 5.1})

    warning_events = [(t, kw) for (t, kw) in emitted if t == "pipeline.warning"]
    assert any(
        kw.get("type") == "live_chunk_drop" and kw.get("count") == 4 for (_t, kw) in warning_events
    ), f"expected pipeline.warning with live_chunk_drop, got: {warning_events}"
