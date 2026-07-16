"""Tests for the system-audio backend abstraction."""

import stat
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

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
