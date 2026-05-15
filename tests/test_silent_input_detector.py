"""Tests for SilentInputDetector — Bug A1.

Reproduces the user-facing failure mode where BlackHole is installed but
not routed (no Multi-Output Device set up in Audio MIDI Setup): the input
stream opens fine and silently delivers nothing. Today the user only finds
out at the end of the meeting when the transcript is empty and the meeting
gets marked 'error' with no explanation. The detector surfaces a warning
within seconds of recording start so the user can fix the routing while
the meeting is still in progress.
"""

import pytest

from src.silent_input_detector import SilentInputDetector


@pytest.fixture
def detector() -> SilentInputDetector:
    # calibration_seconds=0.0 disables the noise-floor window so legacy
    # tests can keep their existing timestamp arithmetic.
    return SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=0.0,
    )


def test_no_alert_for_first_silent_sample(detector):
    """A single silent sample must not fire — that's just the first
    callback before the stream has warmed up."""
    assert detector.observe(system_rms=0.0, now=0.0) is False


def test_no_alert_before_threshold_elapses(detector):
    """Silence for less than the threshold window must not fire."""
    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=5.0) is False
    assert detector.observe(system_rms=0.0, now=9.5) is False


def test_alert_fires_after_threshold_elapses(detector):
    """Silence for >= alert_after_seconds must fire exactly once."""
    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=10.5) is True


def test_alert_fires_only_once_per_session(detector):
    """Once we've alerted, don't keep alerting — one warning is enough,
    spamming would flood the UI / WebSocket."""
    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True
    assert detector.observe(system_rms=0.0, now=20.0) is False
    assert detector.observe(system_rms=0.0, now=100.0) is False


def test_above_threshold_audio_resets_silence_timer(detector):
    """Real audio coming through must reset the silence clock so a brief
    quiet stretch later doesn't re-fire prematurely."""
    detector.observe(system_rms=0.0, now=0.0)
    detector.observe(system_rms=0.0, now=5.0)

    # Audio arrives — clock resets
    detector.observe(system_rms=0.5, now=6.0)

    # New silence stretch — only 4s in, must not fire yet
    detector.observe(system_rms=0.0, now=7.0)
    assert detector.observe(system_rms=0.0, now=15.0) is False

    # 11s of silence after the audio sample — now it can fire
    assert detector.observe(system_rms=0.0, now=18.0) is True


def test_reset_allows_a_fresh_alert(detector):
    """A new recording session (after reset) must be able to alert again."""
    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True

    detector.reset()

    detector.observe(system_rms=0.0, now=100.0)
    assert detector.observe(system_rms=0.0, now=111.0) is True


def test_threshold_is_inclusive_of_floor_noise(detector):
    """RMS values below the configured threshold count as silence; values
    just above count as audio. The 1e-5 default tolerates the noise floor
    of typical interfaces while still treating routed-but-quiet sources
    as audio."""
    # Slightly above threshold: counts as audio, no alert
    detector.observe(system_rms=2e-5, now=0.0)
    assert detector.observe(system_rms=2e-5, now=20.0) is False

    # Reset and try again at threshold boundary
    detector.reset()
    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True


# ---------------------------------------------------------------------------
# Calibration window
# ---------------------------------------------------------------------------


@pytest.fixture
def calibrating_detector() -> SilentInputDetector:
    """Detector with the production default 2s calibration window."""
    return SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=2.0,
    )


def test_calibration_returns_false_during_window(calibrating_detector):
    """While calibrating, observe() must never fire even on silence."""
    assert calibrating_detector.observe(system_rms=0.0, now=0.0) is False
    assert calibrating_detector.observe(system_rms=0.0, now=0.5) is False
    assert calibrating_detector.observe(system_rms=0.0, now=1.5) is False
    # Even very long silence inside calibration must not alert.
    assert calibrating_detector.observe(system_rms=0.0, now=1.99) is False


def test_calibration_floor_raises_effective_threshold(calibrating_detector):
    """If the calibration samples observe non-trivial RMS (e.g. fan noise
    floor at 1e-3), the effective threshold should rise to 1.5x that
    floor, so genuine quiet-but-routed audio at that level still counts
    as silence."""
    # Calibration sees noise floor of 1e-3.
    calibrating_detector.observe(system_rms=1e-3, now=0.0)
    calibrating_detector.observe(system_rms=1e-3, now=1.0)
    # Calibration ends at t=2.0.

    # Post-calibration: an RMS sample at the floor (1e-3) must count
    # as silence, since effective_threshold = 1e-3 * 1.5 = 1.5e-3.
    calibrating_detector.observe(system_rms=1e-3, now=2.5)
    assert calibrating_detector.observe(system_rms=1e-3, now=13.0) is True


def test_calibration_does_not_lower_below_base_threshold(calibrating_detector):
    """Calibration samples that are silent must not drag the effective
    threshold below the configured base — we keep the base as a floor."""
    calibrating_detector.observe(system_rms=0.0, now=0.0)
    calibrating_detector.observe(system_rms=0.0, now=1.0)

    # An RMS slightly above the base threshold should still count as audio.
    calibrating_detector.observe(system_rms=2e-5, now=2.5)
    assert calibrating_detector.observe(system_rms=2e-5, now=15.0) is False


@pytest.mark.parametrize(
    "calibration_samples, observed_floor",
    [
        ([0.0, 0.0, 0.0], 1e-5),  # silent calibration → base threshold
        ([1e-4, 5e-5], 1.5e-4),  # quiet floor → 1.5x of max sample
        ([2e-3, 1e-3, 5e-4], 3e-3),  # noisier floor → 1.5x of max sample
    ],
)
def test_effective_threshold_parametrised(calibration_samples, observed_floor):
    """Effective threshold = max(base, max(samples) * 1.5)."""
    detector = SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=2.0,
    )
    # Spread samples across the calibration window.
    step = 2.0 / max(len(calibration_samples), 1)
    for i, sample in enumerate(calibration_samples):
        detector.observe(system_rms=sample, now=i * step)

    # Sample at exactly the observed_floor should count as silence
    # (i.e. observe should arm the silence timer).
    detector.observe(system_rms=observed_floor, now=2.5)
    assert detector.observe(system_rms=observed_floor, now=13.0) is True


# ---------------------------------------------------------------------------
# Re-arm after recovery
# ---------------------------------------------------------------------------


def test_re_arm_after_recovery_allows_second_alert():
    """After an alert fires, audio recovers, then goes silent again — once
    the re-arm window elapses we should be allowed to fire a second time."""
    detector = SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=0.0,
        re_arm_after_seconds=60.0,
    )

    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True

    # Audio recovers AFTER the re-arm window has elapsed since silence began
    # (silence started at t=0, re_arm=60s → we need now >= 60s with audio).
    assert detector.observe(system_rms=0.5, now=70.0) is False

    # New silence stretch — must be able to alert again.
    detector.observe(system_rms=0.0, now=71.0)
    assert detector.observe(system_rms=0.0, now=82.0) is True


def test_re_arm_does_not_fire_without_recovery():
    """If audio never recovers, re-arm must not re-fire — that would spam
    warnings during a single continuous silent stretch."""
    detector = SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=0.0,
        re_arm_after_seconds=60.0,
    )

    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True
    # Long stretch of continuous silence past the re-arm window — must
    # NOT re-fire because audio never recovered.
    assert detector.observe(system_rms=0.0, now=100.0) is False
    assert detector.observe(system_rms=0.0, now=200.0) is False


def test_re_arm_requires_window_to_elapse():
    """Recovery before the re-arm window elapses must not re-arm the
    detector — otherwise a noisy mic blip could reset state too easily."""
    detector = SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=0.0,
        re_arm_after_seconds=60.0,
    )

    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True

    # Audio briefly recovers at t=15 — way before the 60s re-arm window
    # measured from silence_started_at=0 elapses.
    detector.observe(system_rms=0.5, now=15.0)

    # Silence resumes — must NOT fire again because re-arm window has
    # not elapsed since silence_started_at.
    detector.observe(system_rms=0.0, now=16.0)
    assert detector.observe(system_rms=0.0, now=30.0) is False


@pytest.mark.parametrize(
    "re_arm_after_seconds, recovery_now, second_alert_now, should_fire",
    [
        (60.0, 70.0, 82.0, True),  # recovery after window → re-arm
        (30.0, 15.0, 40.0, False),  # recovery before window → no re-arm
        (5.0, 12.0, 23.0, True),  # short window, post-alert recovery
    ],
)
def test_re_arm_parametrised(re_arm_after_seconds, recovery_now, second_alert_now, should_fire):
    detector = SilentInputDetector(
        alert_after_seconds=10.0,
        silence_threshold=1e-5,
        calibration_seconds=0.0,
        re_arm_after_seconds=re_arm_after_seconds,
    )

    detector.observe(system_rms=0.0, now=0.0)
    assert detector.observe(system_rms=0.0, now=11.0) is True

    detector.observe(system_rms=0.5, now=recovery_now)
    detector.observe(system_rms=0.0, now=recovery_now + 1.0)
    assert detector.observe(system_rms=0.0, now=second_alert_now) is should_fire
