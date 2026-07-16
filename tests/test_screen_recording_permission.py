"""Tests for src/screen_recording_permission.py.

The CoreGraphics binding is faked so the suite never touches real TCC or
raises a Screen Recording prompt.
"""

from types import SimpleNamespace
from unittest.mock import patch

import src.screen_recording_permission as srp


def _fake_cg(*, preflight: bool, request: bool):
    return SimpleNamespace(
        CGPreflightScreenCaptureAccess=lambda: preflight,
        CGRequestScreenCaptureAccess=lambda: request,
    )


def test_status_granted_when_preflight_true():
    with patch.object(srp, "_cg", return_value=_fake_cg(preflight=True, request=True)):
        assert srp.screen_recording_status() == srp.GRANTED


def test_status_denied_when_preflight_false():
    with patch.object(srp, "_cg", return_value=_fake_cg(preflight=False, request=False)):
        assert srp.screen_recording_status() == srp.DENIED


def test_status_unknown_when_binding_unavailable():
    with patch.object(srp, "_cg", return_value=None):
        assert srp.screen_recording_status() == srp.UNKNOWN


def test_request_returns_binding_result():
    with patch.object(srp, "_cg", return_value=_fake_cg(preflight=False, request=True)):
        assert srp.request_screen_recording_access() is True
    with patch.object(srp, "_cg", return_value=_fake_cg(preflight=False, request=False)):
        assert srp.request_screen_recording_access() is False


def test_request_none_when_binding_unavailable():
    with patch.object(srp, "_cg", return_value=None):
        assert srp.request_screen_recording_access() is None


def test_non_darwin_degrades(monkeypatch):
    monkeypatch.setattr(srp.sys, "platform", "linux")
    assert srp._cg() is None
    assert srp.screen_recording_status() == srp.UNKNOWN
    assert srp.request_screen_recording_access() is None
