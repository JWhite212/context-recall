"""Tests for the Teams meeting detector state machine and run loop."""

import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.detector import MeetingEvent, MeetingState, TeamsDetector
from src.utils.config import DetectionConfig

# ------------------------------------------------------------------
# State machine tests
# ------------------------------------------------------------------


class TestDetectorStateMachine:
    """Verify the IDLE → ACTIVE → ENDING state transitions."""

    def test_initial_state_is_idle(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        assert detector.state == MeetingState.IDLE

    def test_single_detection_does_not_start_meeting(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        cb = MagicMock()
        detector.on_meeting_start = cb

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()

        cb.assert_not_called()
        assert detector.state == MeetingState.IDLE

    def test_consecutive_detections_start_meeting(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        cb = MagicMock()
        detector.on_meeting_start = cb

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()

        cb.assert_called_once()
        assert detector.state == MeetingState.ACTIVE

    def test_interrupted_detection_resets_counter(self, detection_config, fake_platform):
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

    def test_meeting_end_requires_consecutive_end_polls(self, detection_config, fake_platform):
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
        # time.time() calls:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):   ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at, cooldown_set
        mock_time.time.side_effect = [0.0, 0.0, 100.0, 199.0, 199.0, 200.0, 200.0, 200.0]

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
        # time.time() calls:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):   ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at, cooldown_set
        mock_time.time.side_effect = [0.0, 0.0, 100.0, 104.0, 104.0, 105.0, 105.0, 105.0]

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
        # time.time() calls:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at=1000
        # tick3 (ACTIVE, ¬act):   ending_started_at=1059, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at=1060, cooldown_set
        mock_time.time.side_effect = [
            0.0,
            0.0,
            1000.0,
            1059.0,
            1059.0,
            1060.0,
            1060.0,
            1060.0,
        ]

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
    def test_state_returns_to_idle_after_end(self, mock_time, detection_config, fake_platform):
        # time.time() calls:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):   ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at, cooldown_set
        mock_time.time.side_effect = [0.0, 0.0, 100.0, 199.0, 199.0, 200.0, 200.0, 200.0]

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

    def test_app_running_and_audio_active_returns_true(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = True
        fake_platform.audio_active = True
        assert detector._is_meeting_active() is True

    def test_app_running_no_audio_falls_back_to_window(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)
        fake_platform.app_running = True
        fake_platform.audio_active = False
        fake_platform.call_window_active = True
        assert detector._is_meeting_active() is True

    def test_app_running_no_audio_no_window_returns_false(self, detection_config, fake_platform):
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

    def test_run_handles_subprocess_error_gracefully(self, detection_config, fake_platform):
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

    def test_run_breaks_on_unexpected_exception(self, detection_config, fake_platform):
        detector = TeamsDetector(detection_config, platform=fake_platform)

        def tick_raises():
            raise RuntimeError("something unexpected")

        detector._tick = tick_raises
        # run() should break out of the loop and return (not hang).
        detector.run()
        assert detector.state == MeetingState.IDLE

    def test_start_callback_exception_stops_loop(self, detection_config, fake_platform):
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
    def test_end_callback_exception_stops_loop(self, mock_time, detection_config, fake_platform):
        # time.time() calls:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):   ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at (callback raises
        #                         before cooldown_set runs)
        mock_time.time.side_effect = [0.0, 0.0, 100.0, 199.0, 199.0, 200.0, 200.0]

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
    def test_rapid_oscillation_no_state_leak(self, mock_time, detection_config, fake_platform):
        # Provide timestamps for two full start/end cycles. Each cycle:
        # tick1 (IDLE, active):   cooldown_check
        # tick2 (IDLE, active):   cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):   ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):   ending_elapsed, ended_at, cooldown_set
        # Cycle 2 has one extra "stale positive" tick that the test
        # asserts must NOT start a meeting (1 cooldown_check call).
        mock_time.time.side_effect = [
            # Cycle 1
            0.0,  # tick1 cooldown
            0.0,  # tick2 cooldown
            100.0,  # tick2 started_at
            199.0,  # tick3 ending_started_at
            199.0,  # tick3 ending_elapsed
            200.0,  # tick4 ending_elapsed
            200.0,  # tick4 ended_at
            200.0,  # tick4 cooldown_set
            # Cycle 2
            201.0,  # IDLE tick (active) cooldown
            201.0,  # IDLE tick (active) cooldown
            300.0,  # IDLE tick (active) started_at
            399.0,  # ACTIVE tick (¬act) ending_started_at
            399.0,  # ACTIVE tick (¬act) ending_elapsed
            400.0,  # ACTIVE tick (¬act) ending_elapsed
            400.0,  # ACTIVE tick (¬act) ended_at
            400.0,  # ACTIVE tick (¬act) cooldown_set
        ]

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


# ------------------------------------------------------------------
# Cooldown tests
# ------------------------------------------------------------------


class TestDetectorCooldown:
    """Verify cooldown prevents split meetings after a meeting ends."""

    @patch("src.detector.time")
    def test_meeting_signals_during_cooldown_are_ignored(self, mock_time, fake_platform):
        """After a meeting ends, new meeting signals within the cooldown
        period should be ignored to prevent split meetings."""
        config = DetectionConfig(
            poll_interval_seconds=0,
            min_meeting_duration_seconds=10,
            required_consecutive_detections=2,
            required_consecutive_end_detections=2,
            min_gap_before_new_meeting=60,
        )
        detector = TeamsDetector(config, platform=fake_platform)
        start_cb = MagicMock()
        end_cb = MagicMock()
        detector.on_meeting_start = start_cb
        detector.on_meeting_end = end_cb

        # time.time() calls in _tick():
        # tick1 (IDLE, active):       cooldown_check (0 < 0 → pass)
        # tick2 (IDLE, active):       cooldown_check, started_at
        # tick3 (ACTIVE, ¬act):       ending_started_at, ending_elapsed
        # tick4 (ACTIVE, ¬act):       ending_elapsed, ended_at,
        #                             cooldown_set = time() + 60
        # tick5 (IDLE, active):       cooldown_check (still in cooldown)
        # tick6 (IDLE, active):       cooldown_check (still in cooldown)
        mock_time.time.side_effect = [
            0.0,  # tick1 cooldown_check
            0.0,  # tick2 cooldown_check
            100.0,  # tick2 started_at
            199.0,  # tick3 ending_started_at
            199.0,  # tick3 ending_elapsed
            200.0,  # tick4 ending_elapsed
            200.0,  # tick4 ended_at
            200.0,  # tick4 cooldown_set (260 = 200 + 60)
            210.0,  # tick5 cooldown_check (210 < 260)
            215.0,  # tick6 cooldown_check (215 < 260)
        ]

        # --- Start and end a meeting ---
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE
        assert start_cb.call_count == 1

        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.IDLE
        assert end_cb.call_count == 1

        # --- Attempt to start a new meeting during cooldown ---
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()  # 210 < 260 -> ignored
        detector._tick()  # 215 < 260 -> ignored
        assert detector.state == MeetingState.IDLE
        assert start_cb.call_count == 1  # No new meeting started.

    @patch("src.detector.time")
    def test_ending_cooldown_commits_after_10s_even_without_enough_polls(
        self, mock_time, fake_platform
    ):
        """A single false-negative poll must not bounce the state
        machine. But after the 10-second ENDING window has elapsed
        with the meeting still inactive, ENDING must commit even if
        ``required_consecutive_end_detections`` has not been reached."""
        config = DetectionConfig(
            poll_interval_seconds=0,
            min_meeting_duration_seconds=0,
            required_consecutive_detections=2,
            # Set absurdly high so the *only* way to commit ENDING is
            # via the 10-second elapsed-time path.
            required_consecutive_end_detections=999,
            min_gap_before_new_meeting=0,
        )
        detector = TeamsDetector(config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # time.time() ledger:
        # tick1 (IDLE, active):   cooldown_check=0
        # tick2 (IDLE, active):   cooldown_check=0, started_at=100
        # tick3 (ACTIVE, ¬act):   ending_started_at=200, ending_elapsed (0s)
        # tick4 (ACTIVE, ¬act):   ending_elapsed (10s exactly) → COMMIT,
        #                         ended_at=210, cooldown_set=210
        mock_time.time.side_effect = [
            0.0,
            0.0,
            100.0,
            200.0,
            200.0,
            210.0,
            210.0,
            210.0,
        ]

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        # First negative poll: timer seeded, no commit yet.
        assert detector.state == MeetingState.ACTIVE
        end_cb.assert_not_called()

        detector._tick()
        # Second negative poll, 10s elapsed — commit even though
        # required_consecutive_end_detections (999) is nowhere near.
        end_cb.assert_called_once()
        assert detector.state == MeetingState.IDLE

    @patch("src.detector.time")
    def test_ending_cooldown_resets_when_meeting_resumes(self, mock_time, fake_platform):
        """If the meeting flips back to active during the 10-second
        window, the ENDING cooldown must reset so a fresh negative
        sequence has to start from scratch."""
        config = DetectionConfig(
            poll_interval_seconds=0,
            min_meeting_duration_seconds=0,
            required_consecutive_detections=2,
            required_consecutive_end_detections=999,
            min_gap_before_new_meeting=0,
        )
        detector = TeamsDetector(config, platform=fake_platform)
        end_cb = MagicMock()
        detector.on_meeting_end = end_cb

        # time.time() ledger:
        # tick1 (IDLE, active):     cooldown_check=0
        # tick2 (IDLE, active):     cooldown_check=0, started_at=100
        # tick3 (ACTIVE, ¬act):     ending_started_at=200, ending_elapsed=0
        # tick4 (ACTIVE, active):   (no time.time calls — just reset)
        # tick5 (ACTIVE, ¬act):     ending_started_at=205, ending_elapsed=0
        # tick6 (ACTIVE, ¬act):     ending_elapsed=4 (<10, no commit)
        mock_time.time.side_effect = [
            0.0,  # tick1 cooldown
            0.0,  # tick2 cooldown
            100.0,  # tick2 started_at
            200.0,  # tick3 ending_started_at
            200.0,  # tick3 ending_elapsed
            205.0,  # tick5 ending_started_at (fresh)
            205.0,  # tick5 ending_elapsed
            209.0,  # tick6 ending_elapsed (only 4s after fresh start)
        ]

        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()
        detector._tick()
        assert detector.state == MeetingState.ACTIVE

        # First negative: timer seeded at 200.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()

        # Meeting flips back to active — timer should reset.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()

        # New negative sequence starts: timer seeded at 205.
        fake_platform.app_running = False
        fake_platform.audio_active = False
        detector._tick()
        detector._tick()
        # Only 4 simulated seconds elapsed since the *fresh* timer
        # started — must NOT have committed yet.
        end_cb.assert_not_called()
        assert detector.state == MeetingState.ACTIVE

    def test_new_meeting_detected_after_cooldown_expires(self, fake_platform):
        """After the cooldown period expires, new meetings should be
        detected normally."""
        config = DetectionConfig(
            poll_interval_seconds=0,
            min_meeting_duration_seconds=0,
            required_consecutive_detections=2,
            required_consecutive_end_detections=2,
            min_gap_before_new_meeting=60,
        )
        detector = TeamsDetector(config, platform=fake_platform)
        start_cb = MagicMock()
        detector.on_meeting_start = start_cb

        # Simulate a cooldown that has already expired.
        import time as real_time

        detector._cooldown_until = real_time.time() - 10

        # New meeting signals should be detected normally.
        fake_platform.app_running = True
        fake_platform.audio_active = True
        detector._tick()  # consecutive=1
        assert detector.state == MeetingState.IDLE
        detector._tick()  # consecutive=2 -> ACTIVE
        assert detector.state == MeetingState.ACTIVE
        assert start_cb.call_count == 1


# ------------------------------------------------------------------
# Per-poll hook and audio passthrough tests
# ------------------------------------------------------------------


def test_on_tick_hook_fires_each_poll():
    from unittest.mock import Mock

    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Idle:
        def is_app_running(self, names):
            return False

        def is_app_using_audio(self, names):
            return False

        def is_call_window_active(self):
            return False

    detector = TeamsDetector(DetectionConfig(), platform=_Idle())
    hook = Mock()
    detector.on_tick = hook

    detector._tick()
    detector._tick()

    assert hook.call_count == 2


def test_on_tick_exception_does_not_break_tick():
    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Idle:
        def is_app_running(self, names):
            return False

        def is_app_using_audio(self, names):
            return False

        def is_call_window_active(self):
            return False

    detector = TeamsDetector(DetectionConfig(), platform=_Idle())
    detector.on_tick = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    detector._tick()  # must not raise


def test_app_using_audio_delegates_to_platform():
    from src.detector import TeamsDetector
    from src.utils.config import DetectionConfig

    class _Fake:
        def __init__(self):
            self.seen = None

        def is_app_running(self, names):
            return True

        def is_app_using_audio(self, names):
            self.seen = names
            return True

        def is_call_window_active(self):
            return False

    fake = _Fake()
    detector = TeamsDetector(DetectionConfig(), platform=fake)
    assert detector.app_using_audio(["zoom.us"]) is True
    assert fake.seen == ["zoom.us"]
