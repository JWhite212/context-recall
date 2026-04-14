"""Tests for the Teams meeting detector state machine and run loop."""

import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.detector import MeetingEvent, MeetingState, TeamsDetector

# ------------------------------------------------------------------
# State machine tests
# ------------------------------------------------------------------


class TestDetectorStateMachine:
    """Verify the IDLE → ACTIVE → ENDING state transitions."""

    def test_initial_state_is_idle(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        assert detector.state == MeetingState.IDLE

    def test_single_detection_does_not_start_meeting(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        cb = MagicMock()
        detector.on_meeting_start = cb

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()

        cb.assert_not_called()
        assert detector.state == MeetingState.IDLE

    def test_consecutive_detections_start_meeting(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        cb = MagicMock()
        detector.on_meeting_start = cb

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()

        cb.assert_called_once()
        assert detector.state == MeetingState.ACTIVE

    def test_interrupted_detection_resets_counter(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        cb = MagicMock()
        detector.on_meeting_start = cb

        # First positive tick.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()

        # Interruption — no meeting signals.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()

        # Counter should have reset; need two fresh consecutive positives.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        cb.assert_not_called()

        detector._tick()
        cb.assert_called_once()
        assert detector.state == MeetingState.ACTIVE

    def test_meeting_end_requires_consecutive_end_polls(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # Move to ACTIVE.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        # One negative poll — not enough to end.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()

        end_cb.assert_not_called()
        assert detector.state == MeetingState.ACTIVE

    @patch("src.detector.time")
    def test_meeting_end_fires_callback(self, mock_time, detection_config, fake_platform):
        # Simulate a meeting that lasts long enough.
        mock_time.time.side_effect = [100.0, 200.0]  # started_at, ended_at

        detector = TeamsDetector(detection_config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # Move to ACTIVE.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()

        # Two consecutive negative polls to end.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()

        end_cb.assert_called_once()
        event = end_cb.call_args[0][0]
        assert event.state == MeetingState.ENDING

    @patch("src.detector.time")
    def test_short_meeting_discarded(self, mock_time, detection_config, fake_platform):
        # started_at=100, ended_at=105 → duration 5s, below min 10s.
        mock_time.time.side_effect = [100.0, 105.0]

        detector = TeamsDetector(detection_config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # Move to ACTIVE.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()

        # End the meeting.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()

        # End callback should NOT have fired — meeting was too short.
        end_cb.assert_not_called()
        # But state should still return to IDLE.
        assert detector.state == MeetingState.IDLE

    def test_end_counter_resets_on_positive(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # Move to ACTIVE.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        # One negative poll.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()

        # Positive poll resets the end counter.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()

        # Another single negative poll — counter started over.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()

        # Should still be ACTIVE; not enough consecutive end polls.
        end_cb.assert_not_called()
        assert detector.state == MeetingState.ACTIVE

    @patch("src.detector.time")
    def test_callback_receives_correct_event_fields(
        self, mock_time, detection_config, fake_platform
    ):
        mock_time.time.side_effect = [1000.0, 1060.0]

        detector = TeamsDetector(detection_config, platform=fake_platform)
        start_cb = MagicMock()
        end_cb = MagicMock()
        detector.on_meeting_start = start_cb
        detector.on_meeting_end = end_cb

        # Start meeting.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()

        start_event: MeetingEvent = start_cb.call_args[0][0]
        assert start_event.state == MeetingState.ACTIVE
        assert start_event.started_at == 1000.0

        # End meeting.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()

        end_event: MeetingEvent = end_cb.call_args[0][0]
        assert end_event.state == MeetingState.ENDING
        assert end_event.started_at == 1000.0
        assert end_event.ended_at == 1060.0
        assert end_event.duration_seconds == pytest.approx(60.0)

    @patch("src.detector.time")
    def test_state_returns_to_idle_after_end(
        self, mock_time, detection_config, fake_platform
    ):
        mock_time.time.side_effect = [100.0, 200.0]

        detector = TeamsDetector(detection_config, platform=fake_platform)

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.IDLE


# ------------------------------------------------------------------
# Detection logic tests
# ------------------------------------------------------------------


class TestDetectorDetectionLogic:
    """Verify _is_meeting_active() detection heuristics."""

    def test_app_not_running_returns_false(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = False
        assert detector._is_meeting_active() is False

    def test_app_running_and_audio_active_returns_true(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = True
        fake_platform.audio_active = True
        assert detector._is_meeting_active() is True

    def test_app_running_no_audio_falls_back_to_window(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = True
        fake_platform.audio_active = False
        fake_platform.call_window_active = True
        assert detector._is_meeting_active() is True

    def test_app_running_no_audio_no_window_returns_false(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = True
        fake_platform.audio_active = False
        fake_platform.call_window_active = False
        assert detector._is_meeting_active() is False

    def test_process_names_passed_through(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        detector._is_meeting_active()
        assert fake_platform.last_process_names == detection_config.process_names


# ------------------------------------------------------------------
# Run loop tests
# ------------------------------------------------------------------


class TestDetectorRunLoop:
    """Verify the blocking run() loop behaviour."""

    def test_run_stops_on_stop_event(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)

        t = threading.Thread(target=detector.run, daemon=True)
        t.start()
        # Give the loop a moment to start, then signal stop.
        import time
        time.sleep(0.05)
        detector.stop()
        t.join(timeout=5)
        assert not t.is_alive()

    def test_run_handles_os_error_gracefully(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        call_count = 0

        def tick_raises():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("device unavailable")
            detector.stop()

        detector._tick = tick_raises
        detector.run()
        # Should have survived the OSError and called _tick at least twice.
        assert call_count >= 2

    def test_run_handles_subprocess_error_gracefully(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        call_count = 0

        def tick_raises():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise subprocess.SubprocessError("pgrep failed")
            detector.stop()

        detector._tick = tick_raises
        detector.run()
        assert call_count >= 2

    def test_run_breaks_on_unexpected_exception(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)

        def tick_raises():
            raise RuntimeError("something unexpected")

        detector._tick = tick_raises
        # run() should break out of the loop and return (not hang).
        detector.run()
        assert detector.state == MeetingState.IDLE

    def test_start_callback_exception_stops_loop(
        self, detection_config, fake_platform
    ):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        start_cb = MagicMock(side_effect=RuntimeError("callback failed"))
        detector.on_meeting_start = start_cb

        fake_platform.app_running = True
        fake_platform.audio_active = True

        # run() blocks, so execute on a thread.
        t = threading.Thread(target=detector.run, daemon=True)
        t.start()
        t.join(timeout=5)

        # The callback exception is caught by the generic except Exception,
        # which breaks the loop — so the thread should have stopped.
        assert not t.is_alive()
        start_cb.assert_called_once()

    @patch("src.detector.time")
    def test_end_callback_exception_stops_loop(
        self, mock_time, detection_config, fake_platform
    ):
        mock_time.time.side_effect = [100.0, 200.0]

        detector = TeamsDetector(detection_config, platform=fake_platform)
        end_cb = MagicMock(side_effect=RuntimeError("end callback failed"))
        detector.on_meeting_end = end_cb

        # Move to ACTIVE via _tick() (bypasses run loop).
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        # Now make detection go negative so the next ticks trigger end.
        fake_platform.app_running = False
        fake_platform.audio_active = False

        # run() will tick and hit the end callback which raises.
        t = threading.Thread(target=detector.run, daemon=True)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive()
        end_cb.assert_called_once()


# ------------------------------------------------------------------
# Rapid oscillation tests
# ------------------------------------------------------------------


class TestDetectorRapidOscillation:
    """Verify state machine counters reset across repeated transitions."""

    @patch("src.detector.time")
    def test_rapid_oscillation_no_state_leak(
        self, mock_time, detection_config, fake_platform
    ):
        # Provide timestamps for two full start/end cycles.
        mock_time.time.side_effect = [100.0, 200.0, 300.0, 400.0]

        detector = TeamsDetector(detection_config, platform=fake_platform)
        start_cb = MagicMock()
        end_cb = MagicMock()
        detector.on_meeting_start = start_cb
        detector.on_meeting_end = end_cb

        # --- Cycle 1: detect meeting ---
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE
        assert start_cb.call_count == 1

        # --- Cycle 1: lose detection → back to IDLE ---
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.IDLE
        assert end_cb.call_count == 1

        # --- Cycle 2: detect meeting again ---
        fake_platform.app_running = True
        fake_platform.audio_active = True
        # A single tick should NOT start a meeting — the counter
        # must have reset to 0 when we returned to IDLE.
        detector._tick()
        assert detector.state == MeetingState.IDLE

        detector._tick()
        assert detector.state == MeetingState.ACTIVE
        assert start_cb.call_count == 2

        # --- Cycle 2: lose detection again ---
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.IDLE
        assert end_cb.call_count == 2
