"""Tests for the system-audio backend abstraction."""

import stat
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

import src.system_audio as sa
from src.audio_capture import AudioCaptureError
from src.utils.config import AudioConfig


def _make_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_resolve_helper_path_frozen(tmp_path):
    macos = tmp_path / "App.app" / "Contents" / "MacOS"
    resources = tmp_path / "App.app" / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    exe = macos / "context-recall-daemon"
    _make_exec(exe)
    helper = resources / sa.HELPER_NAME
    _make_exec(helper)
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
        assert sa.resolve_helper_path() == helper


def test_resolve_helper_path_frozen_missing(tmp_path):
    macos = tmp_path / "App.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "context-recall-daemon"
    _make_exec(exe)
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
        assert sa.resolve_helper_path() is None


def test_resolve_helper_path_dev(tmp_path):
    # __file__ lives at <root>/src/system_audio.py; the dev helper is at
    # <root>/macos/sck-audio-capture/.build/<HELPER_NAME>.
    fake_src = tmp_path / "src" / "system_audio.py"
    fake_src.parent.mkdir(parents=True)
    fake_src.write_text("")
    helper = tmp_path / "macos" / "sck-audio-capture" / ".build" / sa.HELPER_NAME
    helper.parent.mkdir(parents=True)
    _make_exec(helper)
    with patch.object(sa, "__file__", str(fake_src)):
        # not frozen
        with patch.object(sys, "frozen", False, create=True):
            assert sa.resolve_helper_path() == helper


BH_DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2},
    {"name": "MacBook Pro Mic", "max_input_channels": 1},
]


def test_find_blackhole_success(tmp_path):
    """Moved from tests/test_audio_capture.py::TestAudioCaptureDeviceLookup —
    the BlackHole device lookup now lives on BlackHoleSystemCapture."""
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    with patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES):
        idx = backend._find_blackhole("BlackHole")
    assert idx == 0


def test_find_blackhole_not_found(tmp_path):
    """Moved from tests/test_audio_capture.py::TestAudioCaptureDeviceLookup."""
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    with (
        patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES),
        pytest.raises(AudioCaptureError),
    ):
        backend._find_blackhole("NonExistentDevice")


def test_blackhole_backend_finds_device_and_opens_stream(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    out = tmp_path / "meeting_x_system.wav"
    with (
        patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES),
        patch("src.system_audio.sd.InputStream") as MockStream,
        patch("src.system_audio.sf.SoundFile"),
    ):
        backend.start(out)
        # Opened an input stream on the BlackHole index (0).
        assert MockStream.call_args.kwargs["device"] == 0
        MockStream.return_value.start.assert_called_once()
        backend.stop()
        MockStream.return_value.stop.assert_called_once()
        MockStream.return_value.close.assert_called_once()


def test_blackhole_backend_missing_device_sets_error(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    out = tmp_path / "meeting_x_system.wav"
    with patch(
        "src.system_audio.sd.query_devices",
        return_value=[{"name": "MacBook Pro Mic", "max_input_channels": 1}],
    ):
        backend.start(out)
    assert backend.last_error is not None
    assert isinstance(backend.last_error, AudioCaptureError)


def test_blackhole_backend_callback_forwards_data_and_rms(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.BlackHoleSystemCapture(cfg)
    received = []
    backend.on_audio_data = received.append
    out = tmp_path / "meeting_x_system.wav"
    with (
        patch("src.system_audio.sd.query_devices", return_value=BH_DEVICES),
        patch("src.system_audio.sd.InputStream") as MockStream,
        patch("src.system_audio.sf.SoundFile") as MockFile,
    ):
        backend.start(out)
        # Grab the callback sd.InputStream was constructed with and drive it.
        cb = MockStream.call_args.kwargs["callback"]
        stereo = np.full((1024, 2), 0.5, dtype="float32")
        cb(stereo, 1024, None, None)
        assert len(received) == 1
        assert received[0].ndim == 1  # downmixed to mono
        assert backend.latest_rms > 0.0
        MockFile.return_value.write.assert_called()  # wrote mono to the file


def _write_stub_helper(tmp_path, body_python) -> Path:
    """Create an executable python 'helper' honouring the CLI contract.

    Shebang pins to the *current* interpreter (sys.executable) rather than
    `#!/usr/bin/env python3` — the stub imports soundfile/numpy, which live in
    this worktree's venv, not necessarily whatever "python3" resolves to via
    the ambient shell PATH the test happens to run under.
    """
    helper = tmp_path / "stub-helper"
    helper.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body_python))
    helper.chmod(0o755)
    return helper


CAPTURE_STUB = """
    import sys, signal, time
    if "--check-permission" in sys.argv:
        print("granted"); sys.exit(0)
    out = sys.argv[sys.argv.index("--output") + 1]
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    # Write a tiny valid 16k mono PCM16 WAV up front.
    import soundfile as sf, numpy as np
    sf.write(out, np.zeros(16000, dtype="float32"), 16000, subtype="PCM_16")
    while not stop["v"]:
        print("rms=0.010000", flush=True)
        time.sleep(0.05)
    sys.exit(0)
"""

ERROR_STUB = """
    import sys
    if "--check-permission" in sys.argv:
        print("denied"); sys.exit(0)
    sys.stderr.write("error=screen recording denied\\n")
    sys.exit(3)
"""


def test_sck_preflight_reports_granted(tmp_path):
    helper = _write_stub_helper(tmp_path, CAPTURE_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    assert backend.preflight() == "granted"


def test_sck_capture_updates_rms_and_finalises(tmp_path):
    helper = _write_stub_helper(tmp_path, CAPTURE_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    out = tmp_path / "meeting_x_system.wav"
    backend.start(out)
    # Give the reader thread a moment to parse an rms= line.
    for _ in range(40):
        if backend.latest_rms > 0:
            break
        time.sleep(0.05)
    backend.stop()
    assert backend.latest_rms > 0.0
    assert out.exists()
    assert backend.last_error is None


def test_sck_nonzero_exit_sets_error(tmp_path):
    helper = _write_stub_helper(tmp_path, ERROR_STUB)
    cfg = AudioConfig(temp_audio_dir=str(tmp_path))
    backend = sa.ScreenCaptureKitSystemCapture(cfg, helper)
    out = tmp_path / "meeting_x_system.wav"
    backend.start(out)
    # The stub exits near-instantly on the error path. Wait for it to exit on
    # its own (bounded poll, no fixed sleep) so stop()'s SIGTERM doesn't race
    # it and mask the real exit(3) with a "killed by signal" outcome.
    for _ in range(40):
        if backend._proc is not None and backend._proc.poll() is not None:
            break
        time.sleep(0.05)
    backend.stop()
    assert backend.last_error is not None
    assert "screen recording" in (backend.last_warning or "").lower()


def test_select_backend_explicit_blackhole(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="blackhole")
    with patch("src.system_audio.resolve_helper_path", return_value=Path("/x/helper")):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)


def test_select_backend_explicit_sck_without_helper_raises(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="screencapturekit")
    with patch("src.system_audio.resolve_helper_path", return_value=None):
        import pytest

        with pytest.raises(AudioCaptureError):
            sa.select_system_backend(cfg)


def test_select_backend_auto_prefers_sck_when_available(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with (
        patch("src.system_audio.resolve_helper_path", return_value=tmp_path / "helper"),
        patch("src.system_audio._macos_at_least", return_value=True),
    ):
        assert isinstance(sa.select_system_backend(cfg), sa.ScreenCaptureKitSystemCapture)


def test_select_backend_auto_falls_back_without_helper(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with (
        patch("src.system_audio.resolve_helper_path", return_value=None),
        patch("src.system_audio._macos_at_least", return_value=True),
    ):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)


def test_select_backend_auto_falls_back_on_old_macos(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="auto")
    with (
        patch("src.system_audio.resolve_helper_path", return_value=tmp_path / "helper"),
        patch("src.system_audio._macos_at_least", return_value=False),
    ):
        assert isinstance(sa.select_system_backend(cfg), sa.BlackHoleSystemCapture)


def test_select_backend_explicit_sck_with_helper(tmp_path):
    cfg = AudioConfig(temp_audio_dir=str(tmp_path), system_capture_backend="screencapturekit")
    with patch("src.system_audio.resolve_helper_path", return_value=tmp_path / "helper"):
        assert isinstance(sa.select_system_backend(cfg), sa.ScreenCaptureKitSystemCapture)


def test_macos_at_least_parses_major_version():
    with patch("src.system_audio.platform.mac_ver", return_value=("13.2.1", ("", "", ""), "arm64")):
        assert sa._macos_at_least(13) is True
        assert sa._macos_at_least(14) is False


def test_macos_at_least_defensive_on_malformed_version():
    with patch("src.system_audio.platform.mac_ver", return_value=("", ("", "", ""), "arm64")):
        assert sa._macos_at_least(13) is False  # no exception raised
