"""Tests for src/mic_permission.py — macOS microphone TCC introspection.

Production evidence (2026-07-07): the daemon recorded pure digital
silence (-100 dBFS from BOTH devices) and, after a reboot, every
stream.start() failed with PaErrorCode -9986. Both are how CoreAudio
manifests a missing microphone grant: the TCC database still held the
grant for the app's previous name (MeetingMind), so the renamed daemon
had no permission at all and macOS never showed a prompt.

These tests cover the pure decision logic with the Darwin bindings
mocked out. The single Darwin smoke test only READS the status — it
must never trigger a permission prompt.
"""

import sys

import pytest

from src import mic_permission
from src.mic_permission import (
    AUTHORIZED,
    DENIED,
    NOT_DETERMINED,
    RESTRICTED,
    UNKNOWN,
    describe_fix,
    ensure_microphone_access,
)


def _forbid_request(monkeypatch):
    def _boom(**_kwargs):
        raise AssertionError("request_access must not be called")

    monkeypatch.setattr(mic_permission, "request_access", _boom)


class TestEnsureMicrophoneAccess:
    def test_authorized_proceeds_without_prompt(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: AUTHORIZED)
        _forbid_request(monkeypatch)
        status, problem = ensure_microphone_access()
        assert status == AUTHORIZED
        assert problem is None

    def test_denied_returns_actionable_problem(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: DENIED)
        _forbid_request(monkeypatch)
        status, problem = ensure_microphone_access()
        assert status == DENIED
        assert problem is not None
        assert "Microphone" in problem
        assert "System Settings" in problem

    def test_restricted_returns_actionable_problem(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: RESTRICTED)
        _forbid_request(monkeypatch)
        status, problem = ensure_microphone_access()
        assert status == RESTRICTED
        assert problem is not None

    def test_not_determined_prompt_granted(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: NOT_DETERMINED)
        monkeypatch.setattr(mic_permission, "request_access", lambda **kw: True)
        status, problem = ensure_microphone_access()
        assert status == AUTHORIZED
        assert problem is None

    def test_not_determined_prompt_denied(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: NOT_DETERMINED)
        monkeypatch.setattr(mic_permission, "request_access", lambda **kw: False)
        status, problem = ensure_microphone_access()
        assert status == DENIED
        assert problem is not None
        assert "System Settings" in problem

    def test_not_determined_prompt_unanswered(self, monkeypatch):
        """Timeout / mechanism failure: tell the user to answer the dialog."""
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: NOT_DETERMINED)
        monkeypatch.setattr(mic_permission, "request_access", lambda **kw: None)
        status, problem = ensure_microphone_access()
        assert status == NOT_DETERMINED
        assert problem is not None
        assert "Allow" in problem

    def test_not_determined_without_request(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: NOT_DETERMINED)
        _forbid_request(monkeypatch)
        status, problem = ensure_microphone_access(request_if_undetermined=False)
        assert status == NOT_DETERMINED
        assert problem is not None

    def test_unknown_status_never_blocks(self, monkeypatch):
        """Introspection failure must not stop recording (could be a
        false negative on exotic setups) — the silent-input detector
        remains the runtime backstop."""
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: UNKNOWN)
        _forbid_request(monkeypatch)
        status, problem = ensure_microphone_access()
        assert status == UNKNOWN
        assert problem is None

    def test_request_timeout_passed_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(mic_permission, "authorization_status", lambda: NOT_DETERMINED)

        def _fake_request(*, timeout_seconds):
            seen["timeout"] = timeout_seconds
            return True

        monkeypatch.setattr(mic_permission, "request_access", _fake_request)
        ensure_microphone_access(timeout_seconds=42.0)
        assert seen["timeout"] == 42.0


class TestDescribeFix:
    def test_denied_mentions_daemon_and_settings_path(self):
        msg = describe_fix(DENIED)
        assert "context-recall-daemon" in msg
        assert "Privacy & Security" in msg

    def test_mentions_system_audio_needs_it_too(self):
        """BlackHole capture is an *input* stream — recording system audio
        alone still requires the microphone permission. The message must
        say so or users with mic_enabled=false will not understand why
        recording fails."""
        msg = describe_fix(DENIED)
        assert "system audio" in msg.lower()


# Captured at import time, before the conftest autouse guard replaces it.
_REAL_REQUEST_ACCESS = mic_permission.request_access


class TestUsageDescriptionGuard:
    """A frozen binary without NSMicrophoneUsageDescription must never
    call the real request API — macOS KILLS the process for it
    (observed 2026-07-07: launchd crash loop, OS_REASON_TCC)."""

    def test_frozen_without_description_refuses_to_request(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "request_access", _REAL_REQUEST_ACCESS)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(mic_permission, "_has_mic_usage_description", lambda: False)

        def _must_not_run(_timeout):
            raise AssertionError("darwin request must not run without a usage description")

        monkeypatch.setattr(mic_permission, "_request_access_darwin", _must_not_run)
        assert mic_permission.request_access(timeout_seconds=1.0) is None

    def test_frozen_with_description_requests_normally(self, monkeypatch):
        monkeypatch.setattr(mic_permission, "request_access", _REAL_REQUEST_ACCESS)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(mic_permission, "_has_mic_usage_description", lambda: True)
        monkeypatch.setattr(mic_permission, "_request_access_darwin", lambda t: True)
        assert mic_permission.request_access(timeout_seconds=1.0) is True

    def test_unfrozen_dev_run_is_not_gated(self, monkeypatch):
        """A terminal-run dev daemon is covered by the terminal's own
        usage description — the guard must not block it."""
        monkeypatch.setattr(mic_permission, "request_access", _REAL_REQUEST_ACCESS)
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(mic_permission, "_has_mic_usage_description", lambda: False)
        monkeypatch.setattr(mic_permission, "_request_access_darwin", lambda t: False)
        assert mic_permission.request_access(timeout_seconds=1.0) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin-only binding")
class TestDarwinBinding:
    def test_status_read_returns_valid_value(self):
        """Reading the authorization status must never prompt and must
        return one of the known enum values. Uses the underlying Darwin
        implementation directly to bypass the conftest safety patch."""
        value = mic_permission._authorization_status_darwin()
        assert value in {AUTHORIZED, DENIED, RESTRICTED, NOT_DETERMINED, UNKNOWN}
