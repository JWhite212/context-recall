"""Tests for the platform detection abstraction."""

import sys

import pytest

from src.platform.detector import PlatformDetector, create_detector
from src.platform.linux import LinuxDetector
from src.platform.windows import WindowsDetector


def test_create_detector_returns_macos_on_darwin():
    if sys.platform != "darwin":
        pytest.skip("macOS-only test")
    from src.platform.macos import MacOSDetector

    detector = create_detector()
    assert isinstance(detector, MacOSDetector)


def test_macos_detector_implements_protocol():
    if sys.platform != "darwin":
        pytest.skip("macOS-only test")
    from src.platform.macos import MacOSDetector

    detector = MacOSDetector()
    assert isinstance(detector, PlatformDetector)


def test_macos_is_app_running_returns_bool():
    if sys.platform != "darwin":
        pytest.skip("macOS-only test")
    from src.platform.macos import MacOSDetector

    detector = MacOSDetector()
    result = detector.is_app_running(["nonexistent_process_xyz"])
    assert result is False


def test_linux_stub_raises():
    detector = LinuxDetector()
    with pytest.raises(NotImplementedError):
        detector.is_app_running(["test"])
    with pytest.raises(NotImplementedError):
        detector.is_app_using_audio(["test"])
    with pytest.raises(NotImplementedError):
        detector.is_call_window_active()


def test_windows_stub_raises():
    detector = WindowsDetector()
    with pytest.raises(NotImplementedError):
        detector.is_app_running(["test"])
    with pytest.raises(NotImplementedError):
        detector.is_app_using_audio(["test"])
    with pytest.raises(NotImplementedError):
        detector.is_call_window_active()
