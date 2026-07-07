"""Tests for src/audio_preflight.py — pre-flight audio + permission checks."""

from unittest.mock import MagicMock, patch

import pytest

from src.audio_preflight import PreflightReport, run_preflight
from src.utils.config import AudioConfig


def _make_devices(
    *,
    blackhole_input: bool = True,
    blackhole_output_only: bool = False,
    builtin_mic: bool = True,
) -> list[dict]:
    devices: list[dict] = []
    if builtin_mic:
        devices.append(
            {
                "name": "Built-in Microphone",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 44100.0,
            }
        )
    if blackhole_input:
        devices.append(
            {
                "name": "BlackHole 2ch",
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            }
        )
    if blackhole_output_only:
        devices.append(
            {
                "name": "BlackHole speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000.0,
            }
        )
    # Always include a non-input speaker to make sure it's filtered.
    devices.append(
        {
            "name": "Speakers",
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 44100.0,
        }
    )
    return devices


def _stub_default(default_input: int = 0) -> MagicMock:
    default = MagicMock()
    default.device = [default_input, 1]
    return default


@pytest.fixture
def cfg() -> AudioConfig:
    return AudioConfig(
        blackhole_device_name="BlackHole 2ch",
        mic_device_name="",
        mic_enabled=True,
        sample_rate=16000,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_preflight_happy_path_reports_no_errors(cfg):
    devices = _make_devices()

    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert isinstance(report, PreflightReport)
    assert report.blackhole_present is True
    assert report.blackhole_input_candidates == ["BlackHole 2ch"]
    assert report.mic_openable is True
    assert report.microphone_permission_likely is True
    assert report.default_input_index == 0
    assert report.errors == []
    assert report.warnings == []
    fake_stream.start.assert_called_once()
    fake_stream.stop.assert_called_once()
    fake_stream.close.assert_called_once()


def test_preflight_report_to_dict(cfg):
    devices = _make_devices()
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    d = report.to_dict()
    assert d["blackhole_present"] is True
    assert d["blackhole_input_candidates"] == ["BlackHole 2ch"]
    assert d["mic_openable"] is True
    assert d["microphone_permission_likely"] is True
    assert d["default_input_index"] == 0
    assert d["warnings"] == []
    assert d["errors"] == []


# ---------------------------------------------------------------------------
# BlackHole detection
# ---------------------------------------------------------------------------


def test_preflight_blackhole_missing_is_error(cfg):
    devices = _make_devices(blackhole_input=False)
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.blackhole_present is False
    assert report.blackhole_input_candidates == []
    assert any("BlackHole" in e for e in report.errors)


def test_preflight_blackhole_output_only_is_error(cfg):
    """If only output-side BlackHole devices exist, capture cannot work."""
    devices = _make_devices(blackhole_input=False, blackhole_output_only=True)
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.blackhole_present is True
    assert report.blackhole_input_candidates == []
    assert any("input" in e.lower() for e in report.errors)


def test_preflight_configured_blackhole_mismatch_warns(cfg):
    cfg.blackhole_device_name = "BlackHole 16ch"  # Not installed.
    devices = _make_devices()
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.errors == []
    assert any("16ch" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# Mic openability / permissions
# ---------------------------------------------------------------------------


def test_preflight_mic_open_failure_warns_not_errors(cfg):
    """A mic that can't be opened (e.g. permission denied on macOS) is a
    warning, not an error — system audio capture can still proceed."""
    devices = _make_devices()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch(
            "src.audio_preflight.sd.InputStream",
            side_effect=OSError("Permission denied"),
        ),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.mic_openable is False
    assert report.microphone_permission_likely is False
    assert report.errors == []  # System audio can still run.
    assert any("Microphone" in w or "microphone" in w for w in report.warnings)


def test_preflight_mic_disabled_skips_probe(cfg):
    cfg.mic_enabled = False
    devices = _make_devices()
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream) as p,
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.mic_openable is False
    assert report.microphone_permission_likely is False
    assert report.errors == []
    p.assert_not_called()


def test_preflight_configured_mic_not_found_warns(cfg):
    cfg.mic_device_name = "Nonexistent USB Mic"
    devices = _make_devices()
    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert any("Nonexistent USB Mic" in w for w in report.warnings)
    # We did not find the mic, so we didn't probe — mic_openable stays False.
    assert report.mic_openable is False


def test_preflight_no_default_mic_warns(cfg):
    """When mic_device_name is empty and no default input exists, warn."""
    devices = _make_devices(builtin_mic=False)  # Only BlackHole input.
    fake_stream = MagicMock()

    # Default input pointing to -1 → resolves to None.
    default = MagicMock()
    default.device = [-1, -1]

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", default),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.default_input_index is None
    assert any("default microphone" in w.lower() for w in report.warnings)
    assert report.errors == []


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_preflight_query_devices_failure_is_error(cfg):
    with (
        patch(
            "src.audio_preflight.sd.query_devices",
            side_effect=RuntimeError("PortAudio not initialised"),
        ),
    ):
        report = run_preflight(cfg)

    assert any("audio devices" in e for e in report.errors)
    # We never got to the mic probe.
    assert report.mic_openable is False


def test_preflight_default_device_access_failure_does_not_crash(cfg):
    devices = _make_devices()
    fake_stream = MagicMock()

    broken_default = MagicMock()
    type(broken_default).device = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("no default device"))
    )

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", broken_default),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream),
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.default_input_index is None
    # Even though default lookup blew up, we shouldn't have crashed —
    # the report should still report BlackHole presence accurately.
    assert report.blackhole_present is True


def test_preflight_mic_probe_skips_loopback_default(cfg):
    """When the system default input is the BlackHole loopback, the mic
    probe must target a real microphone instead (production regression:
    the 'mic' stream recorded the silent loopback)."""
    devices = _make_devices()  # 0 = Built-in Microphone, 1 = BlackHole 2ch

    fake_stream = MagicMock()

    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(1)),
        patch("src.audio_preflight.sd.InputStream", return_value=fake_stream) as mock_stream,
        patch("src.audio_preflight.time.sleep"),
    ):
        report = run_preflight(cfg)

    assert report.mic_openable is True
    assert mock_stream.call_args.kwargs["device"] == 0


def test_preflight_does_not_refresh_by_default(cfg):
    devices = _make_devices()
    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=MagicMock()),
        patch("src.audio_preflight.time.sleep"),
        patch("src.audio_preflight.refresh_input_devices") as mock_refresh,
    ):
        run_preflight(cfg)
    mock_refresh.assert_not_called()


def test_preflight_refresh_true_reinitialises_devices(cfg):
    devices = _make_devices()
    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=MagicMock()),
        patch("src.audio_preflight.time.sleep"),
        patch("src.audio_preflight.refresh_input_devices") as mock_refresh,
    ):
        run_preflight(cfg, refresh=True)
    mock_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Microphone TCC authorization (the 2026-07-07 silent-recording root cause)
# ---------------------------------------------------------------------------


def _run_with_devices(cfg, monkeypatch, status: str):
    monkeypatch.setattr("src.mic_permission.authorization_status", lambda: status)
    devices = _make_devices()
    with (
        patch("src.audio_preflight.sd.query_devices", return_value=devices),
        patch("src.audio_preflight.sd.default", _stub_default(0)),
        patch("src.audio_preflight.sd.InputStream", return_value=MagicMock()),
        patch("src.audio_preflight.time.sleep"),
    ):
        return run_preflight(cfg)


def test_preflight_reports_microphone_authorization(cfg, monkeypatch):
    report = _run_with_devices(cfg, monkeypatch, "authorized")
    assert report.microphone_authorization == "authorized"
    assert report.errors == []
    assert report.to_dict()["microphone_authorization"] == "authorized"


def test_preflight_denied_permission_is_an_error(cfg, monkeypatch):
    """A denied TCC grant silently zeroes BOTH input streams (or fails
    stream.start() with PortAudio -9986), so it must hard-stop the
    recording with an actionable message — not degrade silently."""
    report = _run_with_devices(cfg, monkeypatch, "denied")
    assert report.microphone_authorization == "denied"
    assert any("Microphone" in e and "System Settings" in e for e in report.errors)


def test_preflight_restricted_permission_is_an_error(cfg, monkeypatch):
    report = _run_with_devices(cfg, monkeypatch, "restricted")
    assert report.errors


def test_preflight_not_determined_is_a_warning_not_error(cfg, monkeypatch):
    """Preflight is side-effect free: it must not fire the TCC prompt
    itself (the orchestrator's gate does that), so an undetermined
    status is a warning that recording will trigger the dialog."""
    report = _run_with_devices(cfg, monkeypatch, "not_determined")
    assert report.microphone_authorization == "not_determined"
    assert report.errors == []
    assert any("permission" in w.lower() for w in report.warnings)


def test_preflight_unknown_permission_is_silent(cfg, monkeypatch):
    """Introspection failure must not add noise — the runtime silent-input
    detector remains the backstop."""
    report = _run_with_devices(cfg, monkeypatch, "unknown")
    assert report.microphone_authorization == "unknown"
    assert report.errors == []
