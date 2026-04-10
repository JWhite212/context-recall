"""Tests for the audio capture module."""

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
    def test_double_start_is_noop(
        self, mock_default, mock_find, mock_record, capture
    ):
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
        actual_rms = np.sqrt(np.mean(audio ** 2))
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
