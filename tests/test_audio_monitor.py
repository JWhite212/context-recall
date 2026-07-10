"""Tests for the BlackHole-only auto-arm audio monitor (pure sustain core)."""

from src.audio_monitor import AudioMonitor


def _monitor():
    # threshold -45 dBFS, sustain 3s. Stream is never opened in these tests.
    return AudioMonitor(
        blackhole_device_name="BlackHole 2ch",
        sample_rate=16000,
        threshold_dbfs=-45.0,
        sustain_seconds=3.0,
    )


def test_inactive_before_any_sample():
    assert _monitor().active() is False


def test_activates_after_sustained_loud_audio():
    m = _monitor()
    # rms 0.1 -> -20 dBFS, well above -45.
    m.observe(0.1, now=0.0)
    assert m.active() is False  # 0s elapsed
    m.observe(0.1, now=2.0)
    assert m.active() is False  # 2s < 3s sustain
    m.observe(0.1, now=3.0)
    assert m.active() is True  # 3s >= sustain


def test_quiet_audio_never_activates():
    m = _monitor()
    # rms 1e-4 -> -80 dBFS, below -45.
    for t in (0.0, 3.0, 6.0):
        m.observe(1e-4, now=t)
    assert m.active() is False


def test_dropping_below_threshold_resets_sustain():
    m = _monitor()
    m.observe(0.1, now=0.0)
    m.observe(0.1, now=3.0)
    assert m.active() is True
    m.observe(1e-4, now=3.5)  # silence
    assert m.active() is False
    m.observe(0.1, now=4.0)  # loud again but clock restarts
    assert m.active() is False
    m.observe(0.1, now=7.0)  # 3s after the restart
    assert m.active() is True


def test_silence_floor_does_not_crash_on_zero_rms():
    m = _monitor()
    m.observe(0.0, now=0.0)  # rms 0 -> -100 dBFS floor
    assert m.active() is False


def test_stop_resets_state():
    m = _monitor()
    m.observe(0.1, now=0.0)
    m.observe(0.1, now=3.0)
    assert m.active() is True
    m.stop()
    assert m.active() is False
