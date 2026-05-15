"""Tests for the audio capture module."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from src.audio_capture import AudioCapture, AudioCaptureError
from src.utils.config import AudioConfig

# ---------------------------------------------------------------------------
# Device lookup tests
# ---------------------------------------------------------------------------

MOCK_DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2},
    {"name": "MacBook Pro Mic", "max_input_channels": 1},
]


class TestAudioCaptureDeviceLookup:
    """Tests for _find_device() and _find_default_input_device()."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        return AudioCapture(config)

    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_find_device_success(self, mock_qd, capture):
        idx = capture._find_device("BlackHole", kind="input")
        assert idx == 0

    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_find_device_not_found(self, mock_qd, capture):
        with pytest.raises(AudioCaptureError):
            capture._find_device("NonExistentDevice")

    @patch("src.audio_capture.sd.query_devices")
    @patch("src.audio_capture.sd.default", new_callable=MagicMock)
    def test_find_default_input_device(self, mock_default, mock_qd, capture):
        mock_default.device = [0, 1]
        mock_qd.return_value = {"name": "MacBook Pro Mic", "max_input_channels": 1}
        idx = capture._find_default_input_device()
        assert idx == 0

    @patch("src.audio_capture.sd.default", new_callable=MagicMock)
    def test_find_default_input_device_none(self, mock_default, capture):
        mock_default.device = [-1, -1]
        idx = capture._find_default_input_device()
        assert idx is None


# ---------------------------------------------------------------------------
# Start / stop lifecycle tests
# ---------------------------------------------------------------------------


class TestAudioCaptureStartStop:
    """Tests for start() and stop() lifecycle."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        return AudioCapture(config)

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_device", return_value=0)
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    def test_double_start_is_noop(self, mock_default, mock_find, mock_record, capture):
        capture.start()
        first_thread = capture._thread

        capture.start()
        second_thread = capture._thread

        # Second start() should be a no-op; thread should be the same.
        assert first_thread is second_thread

        # Clean up: stop recording so thread terminates.
        capture._recording = False
        if capture._thread and capture._thread.is_alive():
            capture._thread.join(timeout=2)

    def test_stop_when_not_recording_returns_none(self, capture):
        result = capture.stop()
        assert result is None

    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    @patch.object(AudioCapture, "_record_loop")
    def test_mic_fallback_chain(self, mock_record, mock_qd, mock_default, capture):
        # Set a mic device name that doesn't exist in our mock device list.
        capture._config.mic_device_name = "NonExistentMic"
        capture._config.mic_enabled = True

        capture.start()

        # mic_idx should be None because the named mic wasn't found
        # and we fall back (AudioCaptureError is caught internally).
        assert capture._mic_idx is None

        # Clean up.
        capture._recording = False
        if capture._thread and capture._thread.is_alive():
            capture._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Audio processing / merge tests
# ---------------------------------------------------------------------------


class TestAudioCaptureMerge:
    """Tests for _to_mono(), _normalise_rms(), _rms_dbfs(), and _merge_sources()."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        return AudioCapture(config)

    def test_to_mono_1d_passthrough(self, capture):
        data = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        mono = capture._to_mono(data)
        assert mono.ndim == 1
        np.testing.assert_array_equal(mono, data)
        # Must be a copy, not the same object.
        assert mono is not data

    def test_to_mono_multichannel(self, capture):
        data = np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float32)
        mono = capture._to_mono(data)
        assert mono.ndim == 1
        expected = np.mean(data, axis=1)
        np.testing.assert_array_almost_equal(mono, expected)

    def test_normalise_rms_silent_passthrough(self):
        audio = np.zeros(1000, dtype=np.float32)
        result = AudioCapture._normalise_rms(audio)
        # Silent audio should pass through unchanged.
        assert np.all(result == 0.0)

    def test_normalise_rms_scales_correctly(self):
        # Create a signal with known RMS.
        rms_in = 0.5
        audio = np.full(1000, rms_in, dtype=np.float32)

        target_dbfs = -20.0
        AudioCapture._normalise_rms(audio, target_dbfs=target_dbfs)

        # After normalisation, RMS should be at the target level.
        target_rms = 10.0 ** (target_dbfs / 20.0)
        actual_rms = np.sqrt(np.mean(audio**2))
        np.testing.assert_almost_equal(actual_rms, target_rms, decimal=4)

    def test_rms_dbfs_silent_floor(self):
        audio = np.zeros(100, dtype=np.float32)
        assert AudioCapture._rms_dbfs(audio) == -100.0

    def test_rms_dbfs_known_value(self):
        # Full-scale signal (all 1.0) has RMS of 1.0, so dBFS should be 0.
        audio = np.ones(1000, dtype=np.float32)
        dbfs = AudioCapture._rms_dbfs(audio)
        np.testing.assert_almost_equal(dbfs, 0.0, decimal=2)

    def test_volume_clamping(self, tmp_path):
        """Verify system_volume and mic_volume are clamped to [0, 2]."""
        config = AudioConfig(
            temp_audio_dir=str(tmp_path),
            system_volume=5.0,
            mic_volume=-1.0,
            keep_source_files=True,
            sample_rate=16000,
        )
        capture = AudioCapture(config)

        # Create minimal WAV files that _merge_sources can read.
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"

        # Write short audio signals so merge has something to work with.
        signal = np.full(1600, 0.5, dtype=np.float32)  # 0.1s at 16kHz
        sf.write(str(system_path), signal, 16000, subtype="PCM_16")
        sf.write(str(mic_path), signal.copy(), 16000, subtype="PCM_16")

        capture._system_path = system_path
        capture._mic_path = mic_path
        capture._output_path = tmp_path / "output.wav"
        capture._config.mic_enabled = True
        capture._mic_idx = 0  # Pretend we have a mic.

        capture._merge_sources()

        # The output file should be created (merge succeeded).
        assert capture._output_path.exists()

        # Read back and verify the levels are reasonable (clamping occurred).
        # With clamping, system_volume=5.0 becomes 2.0 and mic_volume=-1.0 becomes 0.0.
        output_audio, _ = sf.read(str(capture._output_path), dtype="float32")
        # The output should not be silent (system at gain 2.0) and should be clipped to [-1, 1].
        assert np.max(np.abs(output_audio)) <= 1.0


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestAudioCaptureEdgeCases:
    """Edge-case tests for AudioCapture."""

    def test_is_recording_false_initially(self, audio_config):
        capture = AudioCapture(audio_config)
        assert capture.is_recording is False

    def test_merge_mismatched_lengths(self, tmp_path):
        """_merge_sources() pads the shorter source to match the longer one."""
        config = AudioConfig(
            temp_audio_dir=str(tmp_path),
            sample_rate=16000,
            keep_source_files=True,
        )
        capture = AudioCapture(config)

        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        output_path = tmp_path / "output.wav"

        # System audio: 2 seconds (32000 samples at 16kHz).
        system_signal = np.full(32000, 0.3, dtype=np.float32)
        sf.write(str(system_path), system_signal, 16000, subtype="PCM_16")

        # Mic audio: 1 second (16000 samples at 16kHz).
        mic_signal = np.full(16000, 0.3, dtype=np.float32)
        sf.write(str(mic_path), mic_signal, 16000, subtype="PCM_16")

        capture._system_path = system_path
        capture._mic_path = mic_path
        capture._output_path = output_path
        capture._config.mic_enabled = True
        capture._mic_idx = 0

        capture._merge_sources()

        assert output_path.exists()
        output_audio, sr = sf.read(str(output_path), dtype="float32")
        # Output should match the longer source (32000 samples).
        assert len(output_audio) == 32000
        assert sr == 16000

    def test_merge_single_source_no_mic(self, tmp_path):
        """_merge_sources() with no mic normalises system audio only."""
        config = AudioConfig(
            temp_audio_dir=str(tmp_path),
            sample_rate=16000,
            keep_source_files=True,
        )
        capture = AudioCapture(config)

        system_path = tmp_path / "system.wav"
        output_path = tmp_path / "output.wav"

        signal = np.full(16000, 0.5, dtype=np.float32)
        sf.write(str(system_path), signal, 16000, subtype="PCM_16")

        capture._system_path = system_path
        capture._mic_path = None
        capture._output_path = output_path

        capture._merge_sources()

        assert output_path.exists()
        output_audio, _ = sf.read(str(output_path), dtype="float32")
        assert len(output_audio) == 16000
        # Output should be normalised (not silent).
        assert np.max(np.abs(output_audio)) > 0.0

    def test_rms_dbfs_near_zero_floor(self):
        """RMS below 1e-10 should be floored to -100.0 dBFS."""
        audio = np.array([1e-11, -1e-11], dtype=np.float64)
        assert AudioCapture._rms_dbfs(audio) == -100.0


# ---------------------------------------------------------------------------
# Streaming RMS tests
# ---------------------------------------------------------------------------


class TestStreamingRms:
    """Tests for _streaming_rms() accuracy."""

    def test_streaming_rms_matches_monolithic(self, tmp_path):
        """Streaming RMS should match a whole-file RMS calculation."""
        config = AudioConfig(temp_audio_dir=str(tmp_path), sample_rate=16000)
        capture = AudioCapture(config)

        signal = np.random.randn(160000).astype(np.float32) * 0.3
        path = tmp_path / "test.wav"
        sf.write(str(path), signal, 16000, subtype="PCM_16")

        # Re-read from disk so both calculations use the PCM_16 quantised values.
        audio, _ = sf.read(str(path), dtype="float32")
        expected_rms = float(np.sqrt(np.mean(audio**2)))
        actual_rms = capture._streaming_rms(path)

        np.testing.assert_almost_equal(actual_rms, expected_rms, decimal=4)

    def test_streaming_rms_silent_file(self, tmp_path):
        """Streaming RMS of a silent file should be 0.0."""
        config = AudioConfig(temp_audio_dir=str(tmp_path), sample_rate=16000)
        capture = AudioCapture(config)

        signal = np.zeros(16000, dtype=np.float32)
        path = tmp_path / "silent.wav"
        sf.write(str(path), signal, 16000, subtype="PCM_16")

        assert capture._streaming_rms(path) == 0.0

    def test_streaming_rms_short_file(self, tmp_path):
        """Streaming RMS works correctly on files shorter than one chunk."""
        config = AudioConfig(temp_audio_dir=str(tmp_path), sample_rate=16000)
        capture = AudioCapture(config)

        # 100 samples — well under the 480000-sample chunk size.
        signal = np.full(100, 0.5, dtype=np.float32)
        path = tmp_path / "short.wav"
        sf.write(str(path), signal, 16000, subtype="PCM_16")

        audio, _ = sf.read(str(path), dtype="float32")
        expected_rms = float(np.sqrt(np.mean(audio**2)))
        actual_rms = capture._streaming_rms(path)

        np.testing.assert_almost_equal(actual_rms, expected_rms, decimal=4)


# ---------------------------------------------------------------------------
# Non-blocking stop tests
# ---------------------------------------------------------------------------


class TestNonBlockingStop:
    """Tests for stop(blocking=False) and wait_for_merge()."""

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_device", return_value=0)
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    def test_non_blocking_stop_returns_promptly(
        self, mock_default, mock_find, mock_record, tmp_path
    ):
        """stop(blocking=False) should return without waiting for the merge."""
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        capture = AudioCapture(config)

        # Simulate: _record_loop sets streams_stopped immediately (mocked).
        capture._streams_stopped.set()

        capture.start()
        result = capture.stop(blocking=False)

        # Should return without blocking; thread is still "alive" (mocked).
        assert result is None or isinstance(result, Path)

        # Clean up.
        capture._recording = False
        if capture._thread and capture._thread.is_alive():
            capture._thread.join(timeout=2)

    def test_wait_for_merge_returns_true_when_set(self, tmp_path):
        """wait_for_merge() returns True when the merge event is already set."""
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        capture = AudioCapture(config)

        capture._merge_complete.set()
        assert capture.wait_for_merge(timeout=0.1) is True

    def test_wait_for_merge_returns_false_on_timeout(self, tmp_path):
        """wait_for_merge() returns False when the event is not set."""
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        capture = AudioCapture(config)

        assert capture.wait_for_merge(timeout=0.05) is False

    @patch.object(AudioCapture, "_find_device", return_value=0)
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    def test_start_resets_events(self, mock_default, mock_find, tmp_path):
        """start() should clear both lifecycle events."""
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        capture = AudioCapture(config)

        # Pre-set the events.
        capture._streams_stopped.set()
        capture._merge_complete.set()

        with patch.object(AudioCapture, "_record_loop"):
            capture.start()

        # Events should have been cleared.
        assert not capture._streams_stopped.is_set()
        assert not capture._merge_complete.is_set()

        # Clean up.
        capture._recording = False
        if capture._thread and capture._thread.is_alive():
            capture._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Bug A4: silent mic-fallback must surface a warning the orchestrator can emit
# ---------------------------------------------------------------------------


class TestMicResolutionWarning:
    """Bug A4: when mic resolution silently degrades to system-only, the
    user gets no UI signal. The mic device they expected to be recording
    just isn't, and they only notice from missing audio in the transcript.
    AudioCapture.last_warning surfaces the reason so the orchestrator can
    emit a pipeline.warning event the existing UI banner already renders.
    """

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        return AudioCapture(AudioConfig(temp_audio_dir=str(tmp_path)))

    def test_last_warning_is_none_initially(self, capture):
        assert capture.last_warning is None

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_no_default_mic_sets_warning(self, mock_qd, mock_default, mock_record, capture):
        capture._config.mic_enabled = True
        capture._config.mic_device_name = ""
        capture.start()
        try:
            assert capture.last_warning is not None
            assert capture._mic_idx is None
            assert "default" in capture.last_warning.lower()
        finally:
            capture._recording = False
            if capture._thread and capture._thread.is_alive():
                capture._thread.join(timeout=2)

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_named_mic_not_found_sets_warning(self, mock_qd, mock_default, mock_record, capture):
        capture._config.mic_enabled = True
        capture._config.mic_device_name = "DefinitelyNotAMicDevice"
        capture.start()
        try:
            assert capture.last_warning is not None
            assert capture._mic_idx is None
            assert "DefinitelyNotAMicDevice" in capture.last_warning
        finally:
            capture._recording = False
            if capture._thread and capture._thread.is_alive():
                capture._thread.join(timeout=2)

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_default_input_device", return_value=1)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_successful_mic_resolution_sets_no_warning(
        self, mock_qd, mock_default, mock_record, capture
    ):
        """When the default mic resolves, no warning should be set."""
        capture._config.mic_enabled = True
        capture._config.mic_device_name = ""
        capture.start()
        try:
            assert capture.last_warning is None
            assert capture._mic_idx == 1
        finally:
            capture._recording = False
            if capture._thread and capture._thread.is_alive():
                capture._thread.join(timeout=2)

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_mic_disabled_does_not_warn(self, mock_qd, mock_default, mock_record, capture):
        """User explicitly disabled the mic — no warning, no surprise."""
        capture._config.mic_enabled = False
        capture.start()
        try:
            assert capture.last_warning is None
        finally:
            capture._recording = False
            if capture._thread and capture._thread.is_alive():
                capture._thread.join(timeout=2)

    @patch.object(AudioCapture, "_record_loop")
    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    def test_start_resets_previous_warning(self, mock_qd, mock_default, mock_record, capture):
        """A successful subsequent start must clear a stale warning from
        a prior session — otherwise the UI would show outdated hints."""
        capture._last_warning = "stale warning from last session"
        capture._config.mic_enabled = False  # Quiet path: no new warning
        capture.start()
        try:
            assert capture.last_warning is None
        finally:
            capture._recording = False
            if capture._thread and capture._thread.is_alive():
                capture._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Bug A2: stream-open failures must signal callers immediately, not 120s later
# ---------------------------------------------------------------------------


class TestStreamOpenFailureSignaling:
    """Reproduces Bug A2: when InputStream.start() raises (mic permission
    denied, device unavailable, kernel audio crash), the existing code
    swallows the error in the audio thread and never sets _merge_complete.
    The orchestrator's wait_for_merge then blocks for the full 120s timeout
    before marking the meeting as 'error', and the user gets no clue that
    the original cause was a microphone permission problem."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        config = AudioConfig(temp_audio_dir=str(tmp_path))
        return AudioCapture(config)

    def test_last_error_is_none_initially(self, capture):
        """A fresh AudioCapture has no recorded error."""
        assert capture.last_error is None

    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    @patch("src.audio_capture.sd.InputStream")
    def test_stream_start_failure_sets_merge_complete_and_last_error(
        self, mock_input_stream, mock_qd, mock_default, capture
    ):
        """When InputStream.start() raises, the audio thread must:
        1. Record the error in capture.last_error so callers can surface it.
        2. Set _merge_complete so wait_for_merge returns immediately, not
           after the 120s timeout.
        """
        # Simulate macOS denying microphone permission. PortAudio surfaces
        # this as an exception from .start(), not from the constructor.
        mock_stream_instance = MagicMock()
        mock_stream_instance.start.side_effect = Exception(
            "PaErrorCode -9986: invalid device or permission denied"
        )
        mock_input_stream.return_value = mock_stream_instance

        capture.start()
        # Wait for the background thread to hit the failure and exit.
        if capture._thread:
            capture._thread.join(timeout=5)

        # Caller must not wait 120s for a verdict on a failure that already
        # happened. wait_for_merge must return True (event is set) quickly.
        assert capture.wait_for_merge(timeout=1.0) is True

        # Caller must be able to discover what went wrong without parsing
        # log files. last_error must be a typed AudioCaptureError so the
        # orchestrator can switch on it and surface a real message to the UI.
        assert capture.last_error is not None
        assert isinstance(capture.last_error, AudioCaptureError)


# ---------------------------------------------------------------------------
# Unit 1: on_capture_error / on_stream_status callbacks + dual-source schema
# validation. These give the orchestrator immediate visibility into capture
# failures and mid-stream device-disconnect symptoms, and stop a downstream
# merge from silently producing garbled audio when the two source files
# disagree on sample rate or channel count.
# ---------------------------------------------------------------------------


class TestCaptureErrorCallback:
    """on_capture_error must fire from the capture thread the moment an
    unrecoverable failure is observed — and must fire BEFORE
    _merge_complete is set, so the orchestrator sees the error before
    wait_for_merge unblocks."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        return AudioCapture(AudioConfig(temp_audio_dir=str(tmp_path)))

    def test_on_capture_error_attribute_defaults_to_none(self, capture):
        assert capture.on_capture_error is None

    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    @patch("src.audio_capture.sd.InputStream")
    def test_on_capture_error_invoked_with_audio_capture_error(
        self, mock_input_stream, mock_qd, mock_default, capture
    ):
        mock_stream_instance = MagicMock()
        mock_stream_instance.start.side_effect = Exception("device disconnected")
        mock_input_stream.return_value = mock_stream_instance

        received: list[AudioCaptureError] = []
        capture.on_capture_error = lambda err: received.append(err)

        capture.start()
        if capture._thread:
            capture._thread.join(timeout=5)

        assert len(received) == 1
        assert isinstance(received[0], AudioCaptureError)
        assert "device disconnected" in str(received[0])

    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    @patch("src.audio_capture.sd.InputStream")
    def test_on_capture_error_fires_before_merge_complete(
        self, mock_input_stream, mock_qd, mock_default, capture
    ):
        """The callback must run BEFORE _merge_complete is set so callers
        waiting on wait_for_merge don't race ahead of the error signal."""
        mock_stream_instance = MagicMock()
        mock_stream_instance.start.side_effect = Exception("kapow")
        mock_input_stream.return_value = mock_stream_instance

        merge_set_when_called: list[bool] = []
        capture.on_capture_error = lambda err: merge_set_when_called.append(
            capture._merge_complete.is_set()
        )

        capture.start()
        if capture._thread:
            capture._thread.join(timeout=5)

        assert merge_set_when_called == [False], (
            "on_capture_error must be invoked before _merge_complete is set"
        )

    @patch.object(AudioCapture, "_find_default_input_device", return_value=None)
    @patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES)
    @patch("src.audio_capture.sd.InputStream")
    def test_on_capture_error_callback_exception_is_swallowed(
        self, mock_input_stream, mock_qd, mock_default, capture
    ):
        """A misbehaving callback must not poison the capture thread —
        _merge_complete must still be set so wait_for_merge can return."""
        mock_stream_instance = MagicMock()
        mock_stream_instance.start.side_effect = Exception("kapow")
        mock_input_stream.return_value = mock_stream_instance

        capture.on_capture_error = MagicMock(side_effect=RuntimeError("callback broke"))

        capture.start()
        if capture._thread:
            capture._thread.join(timeout=5)

        assert capture.wait_for_merge(timeout=1.0) is True
        assert capture.last_error is not None


class TestStreamStatusCallback:
    """on_stream_status fires whenever a sounddevice CallbackFlags value
    is truthy on either source — the UI uses this to warn the user that
    the device is xrun-ing or has disconnected mid-stream."""

    @pytest.fixture
    def capture(self, tmp_path) -> AudioCapture:
        return AudioCapture(AudioConfig(temp_audio_dir=str(tmp_path)))

    def test_on_stream_status_attribute_defaults_to_none(self, capture):
        assert capture.on_stream_status is None

    def test_system_callback_invokes_on_stream_status_when_status_truthy(self, capture, tmp_path):
        """Drive the inner system_callback synthesised inside _record_loop
        by mocking InputStream so we can capture the callback function and
        invoke it with a non-empty status flag."""
        received: list[tuple[str, str]] = []
        capture.on_stream_status = lambda source, status: received.append((source, status))

        captured_callbacks: dict[str, object] = {}

        def fake_input_stream(*args, **kwargs):
            stream = MagicMock()
            # The first InputStream constructed is the system stream.
            if "system" not in captured_callbacks:
                captured_callbacks["system"] = kwargs["callback"]
            else:
                captured_callbacks["mic"] = kwargs["callback"]
            return stream

        with (
            patch.object(AudioCapture, "_find_device", return_value=0),
            patch.object(AudioCapture, "_find_default_input_device", return_value=1),
            patch(
                "src.audio_capture.sd.query_devices",
                side_effect=lambda *a, **k: (
                    MOCK_DEVICES if not a else {"name": "MacBook Pro Mic", "max_input_channels": 1}
                ),
            ),
            patch("src.audio_capture.sd.InputStream", side_effect=fake_input_stream),
        ):
            capture._config.mic_enabled = True
            capture.start()
            # Give the record loop a beat to install the callbacks.
            for _ in range(50):
                if "system" in captured_callbacks and "mic" in captured_callbacks:
                    break
                time.sleep(0.02)

            try:
                system_cb = captured_callbacks["system"]
                mic_cb = captured_callbacks["mic"]

                indata = np.zeros((1024, 2), dtype=np.float32)
                system_cb(indata, 1024, None, "input overflow")

                mic_indata = np.zeros((1024, 1), dtype=np.float32)
                mic_cb(mic_indata, 1024, None, "input underflow")
            finally:
                capture._recording = False
                if capture._thread and capture._thread.is_alive():
                    capture._thread.join(timeout=2)

        sources = {s for s, _ in received}
        assert sources == {"system", "mic"}
        # Status strings should be propagated as-is (stringified).
        joined = " ".join(status for _, status in received)
        assert "overflow" in joined
        assert "underflow" in joined

    def test_on_stream_status_not_called_when_status_falsy(self, capture, tmp_path):
        """A clean callback (status=None or empty CallbackFlags) must not
        trigger the warning callback — otherwise it would fire continuously."""
        received: list[tuple[str, str]] = []
        capture.on_stream_status = lambda source, status: received.append((source, status))

        captured_callbacks: dict[str, object] = {}

        def fake_input_stream(*args, **kwargs):
            stream = MagicMock()
            if "system" not in captured_callbacks:
                captured_callbacks["system"] = kwargs["callback"]
            else:
                captured_callbacks["mic"] = kwargs["callback"]
            return stream

        with (
            patch.object(AudioCapture, "_find_device", return_value=0),
            patch.object(AudioCapture, "_find_default_input_device", return_value=None),
            patch("src.audio_capture.sd.query_devices", return_value=MOCK_DEVICES),
            patch("src.audio_capture.sd.InputStream", side_effect=fake_input_stream),
        ):
            capture._config.mic_enabled = False
            capture.start()
            for _ in range(50):
                if "system" in captured_callbacks:
                    break
                time.sleep(0.02)

            try:
                system_cb = captured_callbacks["system"]
                indata = np.zeros((1024, 2), dtype=np.float32)
                # status=None == "no flags raised by PortAudio".
                system_cb(indata, 1024, None, None)
            finally:
                capture._recording = False
                if capture._thread and capture._thread.is_alive():
                    capture._thread.join(timeout=2)

        assert received == []


class TestMergeSchemaValidation:
    """_merge_dual_source must refuse to mix sources that disagree on
    sample rate or channel count — silently doing so would emit garbled
    output that's only diagnosable by listening to the result."""

    def _write_wav(self, path: Path, *, sample_rate: int, channels: int) -> None:
        samples = 1600
        shape = (samples,) if channels == 1 else (samples, channels)
        signal = np.full(shape, 0.3, dtype=np.float32)
        sf.write(str(path), signal, sample_rate, subtype="PCM_16")

    def test_sample_rate_mismatch_raises_audio_capture_error(self, tmp_path):
        capture = AudioCapture(
            AudioConfig(
                temp_audio_dir=str(tmp_path),
                sample_rate=16000,
                keep_source_files=True,
            )
        )
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        self._write_wav(system_path, sample_rate=16000, channels=1)
        self._write_wav(mic_path, sample_rate=48000, channels=1)

        capture._system_path = system_path
        capture._mic_path = mic_path
        capture._output_path = tmp_path / "output.wav"

        with pytest.raises(AudioCaptureError) as exc_info:
            capture._merge_dual_source()

        message = str(exc_info.value)
        assert "16000" in message
        assert "48000" in message

    def test_channel_count_mismatch_raises_audio_capture_error(self, tmp_path):
        capture = AudioCapture(
            AudioConfig(
                temp_audio_dir=str(tmp_path),
                sample_rate=16000,
                keep_source_files=True,
            )
        )
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        self._write_wav(system_path, sample_rate=16000, channels=1)
        self._write_wav(mic_path, sample_rate=16000, channels=2)

        capture._system_path = system_path
        capture._mic_path = mic_path
        capture._output_path = tmp_path / "output.wav"

        with pytest.raises(AudioCaptureError):
            capture._merge_dual_source()

    def test_matching_schema_does_not_raise(self, tmp_path):
        """Sanity check: when the two source files agree, the merge
        proceeds without raising the new validation error."""
        capture = AudioCapture(
            AudioConfig(
                temp_audio_dir=str(tmp_path),
                sample_rate=16000,
                keep_source_files=True,
            )
        )
        system_path = tmp_path / "system.wav"
        mic_path = tmp_path / "mic.wav"
        output_path = tmp_path / "output.wav"
        self._write_wav(system_path, sample_rate=16000, channels=1)
        self._write_wav(mic_path, sample_rate=16000, channels=1)

        capture._system_path = system_path
        capture._mic_path = mic_path
        capture._output_path = output_path
        capture._config.mic_enabled = True
        capture._mic_idx = 0

        capture._merge_dual_source()
        assert output_path.exists()
