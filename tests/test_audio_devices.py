"""Tests for src/audio_devices.py — shared input-device resolution.

Regression context: the daemon recorded a meeting with the "microphone"
stream opened on 'BlackHole 2ch' because macOS's default input device was
set to the loopback. The mic must never silently resolve to a virtual /
loopback device.
"""

from src.audio_devices import is_virtual_input, resolve_default_mic_index

DEVICES = [
    {"name": "BlackHole 2ch", "max_input_channels": 2},  # 0 loopback
    {"name": "DELL S2725QC", "max_input_channels": 0},  # 1 output-only
    {"name": "Microsoft Teams Audio", "max_input_channels": 1},  # 2 virtual
    {"name": "Jabra Link 390", "max_input_channels": 1},  # 3 real headset
    {"name": "MacBook Pro Microphone", "max_input_channels": 1},  # 4 real mic
    {"name": "Aggregate Device", "max_input_channels": 1},  # 5 virtual
    {"name": "Multi-Output System & Mic", "max_input_channels": 0},  # 6 output-only
]


class TestIsVirtualInput:
    def test_loopback_and_virtual_names_are_virtual(self):
        for name in (
            "BlackHole 2ch",
            "BlackHole 16ch",
            "Microsoft Teams Audio",
            "Aggregate Device",
            "Multi-Output Device",
            "Loopback Audio",
            "Soundflower (2ch)",
        ):
            assert is_virtual_input(name), name

    def test_real_devices_are_not_virtual(self):
        for name in ("Jabra Link 390", "MacBook Pro Microphone", "AT2020USB+"):
            assert not is_virtual_input(name), name


class TestResolveDefaultMicIndex:
    def test_real_default_input_is_used(self):
        assert resolve_default_mic_index(DEVICES, default_index=3) == 3

    def test_loopback_default_falls_back_to_real_mic(self):
        # The exact production failure: default input == BlackHole.
        idx = resolve_default_mic_index(DEVICES, default_index=0)
        assert idx is not None
        assert idx != 0
        assert not is_virtual_input(DEVICES[idx]["name"])

    def test_fallback_prefers_device_named_microphone(self):
        assert resolve_default_mic_index(DEVICES, default_index=0) == 4

    def test_no_default_scans_for_real_mic(self):
        assert resolve_default_mic_index(DEVICES, default_index=None) == 4

    def test_excluded_index_is_never_chosen(self):
        # Exclude the capture loopback even if its name wouldn't match the
        # virtual patterns (e.g. a custom-named loopback from config).
        devices = [
            {"name": "My Custom Loopback", "max_input_channels": 2},
            {"name": "Jabra Link 390", "max_input_channels": 1},
        ]
        assert resolve_default_mic_index(devices, default_index=0, exclude={0}) == 1

    def test_only_virtual_inputs_returns_none(self):
        devices = [
            {"name": "BlackHole 2ch", "max_input_channels": 2},
            {"name": "Microsoft Teams Audio", "max_input_channels": 1},
        ]
        assert resolve_default_mic_index(devices, default_index=0) is None

    def test_no_input_devices_returns_none(self):
        devices = [{"name": "Speakers", "max_input_channels": 0}]
        assert resolve_default_mic_index(devices, default_index=None) is None
