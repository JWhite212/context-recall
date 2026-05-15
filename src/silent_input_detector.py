"""Detect a system-audio source that is open but silent.

Reproduces the user-facing symptom of Bug A1: BlackHole is installed (so
the input stream opens fine) but the user has not built a Multi-Output
Device in Audio MIDI Setup, so no system audio is actually routed to it.
The stream delivers a steady stream of zero samples and the user only
discovers the problem at the end of the meeting when the transcript is
empty.

The detector is a small state machine that the orchestrator feeds with
each audio.level callback. After ALERT_AFTER_SECONDS of continuous
sub-threshold input, observe() returns True; the orchestrator turns
that into a pipeline.warning event. After the silent stretch elapses
the re-arm window, a recovery sample re-arms the detector so a later
silent stretch in the same session can alert again (e.g. BlackHole
routing breaks mid-meeting after a system audio toggle).

The detector also runs a short noise-floor calibration window at the
start of each session. Samples observed inside that window raise the
effective silence threshold to ``max(base, observed_floor * 1.5)`` so
the detector tolerates an interface noise floor that sits above the
default base threshold.

Time is injected so the detector is pure-function testable; production
callers pass time.monotonic().
"""

from __future__ import annotations


class SilentInputDetector:
    """Silence detector for a single audio source with re-arm + calibration."""

    def __init__(
        self,
        *,
        alert_after_seconds: float = 10.0,
        silence_threshold: float = 1e-5,
        calibration_seconds: float = 2.0,
        re_arm_after_seconds: float = 60.0,
    ) -> None:
        self._alert_after_seconds = alert_after_seconds
        self._silence_threshold = silence_threshold
        self._calibration_seconds = calibration_seconds
        self._re_arm_after_seconds = re_arm_after_seconds

        self._silence_started_at: float | None = None
        self._alerted: bool = False

        # Calibration state. ``_calibrating`` is True until the noise-floor
        # window closes; ``_effective_threshold`` holds the post-calibration
        # threshold (defaults to the base so a 0s window short-circuits).
        self._calibrating: bool = calibration_seconds > 0.0
        self._calibration_started_at: float | None = None
        self._calibration_samples: list[float] = []
        self._effective_threshold: float = silence_threshold

    def reset(self) -> None:
        """Clear state for a new recording session."""
        self._silence_started_at = None
        self._alerted = False
        self._calibrating = self._calibration_seconds > 0.0
        self._calibration_started_at = None
        self._calibration_samples = []
        self._effective_threshold = self._silence_threshold

    def observe(self, *, system_rms: float, now: float) -> bool:
        """Record an audio level sample.

        Returns True iff this sample crosses the silence threshold for
        the first time in the current arm cycle. After the re-arm window
        elapses and audio recovers, the detector clears its one-shot
        latch so a subsequent silence stretch can fire again.
        """
        if self._calibrating:
            if self._calibration_started_at is None:
                self._calibration_started_at = now
            if now - self._calibration_started_at < self._calibration_seconds:
                self._calibration_samples.append(system_rms)
                return False
            # Window closed: lock in the effective threshold once.
            if self._calibration_samples:
                observed_floor = max(self._calibration_samples) * 1.5
                self._effective_threshold = max(self._silence_threshold, observed_floor)
            self._calibration_samples = []
            self._calibrating = False

        is_silent = system_rms <= self._effective_threshold

        # Re-arm: if we previously alerted, the silent stretch has lasted
        # at least re_arm_after_seconds, and audio has recovered, clear
        # the latch so a future silent stretch can fire again.
        if (
            self._alerted
            and not is_silent
            and self._silence_started_at is not None
            and now - self._silence_started_at >= self._re_arm_after_seconds
        ):
            self._alerted = False
            self._silence_started_at = None
            return False

        if self._alerted:
            # Still latched; track the silence clock for ongoing state
            # but suppress further alerts.
            if not is_silent:
                self._silence_started_at = None
            elif self._silence_started_at is None:
                self._silence_started_at = now
            return False

        if not is_silent:
            # Real audio — clear the silence clock.
            self._silence_started_at = None
            return False

        if self._silence_started_at is None:
            # First silent sample — start the clock.
            self._silence_started_at = now
            return False

        if now - self._silence_started_at >= self._alert_after_seconds:
            self._alerted = True
            return True

        return False
