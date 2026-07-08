"""Tests for src/audio_routing.py — automatic system-audio routing.

The AudioRouter logic is exercised against a FakeBackend; the real
CoreAudioBackend is never touched by the suite (conftest forces it
unavailable) so tests can never mutate the host's audio configuration.
"""

import pytest

from src.audio_routing import (
    MANAGED_DEVICE_NAME,
    MANAGED_DEVICE_UID,
    AudioRouter,
    CoreAudioError,
    RoutingResult,
)

JABRA_UID = "usb:jabra-link-390"
SPEAKERS_UID = "BuiltInSpeakerDevice"
BLACKHOLE_UID = "BlackHole2ch_UID"


class FakeBackend:
    """In-memory CoreAudio double: devices are dicts keyed by int id."""

    def __init__(self, devices: dict[int, dict], default_output: int):
        self.devices = devices
        self._default_output = default_output
        self.created: list[dict] = []
        self.destroyed: list[int] = []
        self._next_id = max(devices) + 1 if devices else 1

    def available(self) -> bool:
        return True

    def default_output_device(self) -> int:
        return self._default_output

    def set_default_output_device(self, device_id: int) -> None:
        if device_id not in self.devices:
            raise CoreAudioError(f"no such device: {device_id}")
        self._default_output = device_id

    def device_name(self, device_id: int) -> str:
        return self.devices[device_id]["name"]

    def device_uid(self, device_id: int) -> str:
        return self.devices[device_id]["uid"]

    def all_device_ids(self) -> list[int]:
        return list(self.devices)

    def has_output_streams(self, device_id: int) -> bool:
        return self.devices[device_id].get("output", True)

    def subdevice_uids(self, device_id: int) -> list[str] | None:
        return self.devices[device_id].get("subs")

    def create_multi_output(self, name, uid, subdevice_uids, master_uid) -> int:
        dev_id = self._next_id
        self._next_id += 1
        self.devices[dev_id] = {
            "name": name,
            "uid": uid,
            "subs": list(subdevice_uids),
            "output": True,
        }
        self.created.append({"id": dev_id, "name": name, "uid": uid, "subs": subdevice_uids})
        return dev_id

    def destroy_aggregate(self, device_id: int) -> None:
        del self.devices[device_id]
        self.destroyed.append(device_id)


def _standard_devices() -> dict[int, dict]:
    return {
        1: {"name": "Jabra Link 390", "uid": JABRA_UID, "output": True},
        2: {"name": "BlackHole 2ch", "uid": BLACKHOLE_UID, "output": True},
        3: {"name": "Mac mini Speakers", "uid": SPEAKERS_UID, "output": True},
    }


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(_standard_devices(), default_output=1)


def _router(backend: FakeBackend) -> AudioRouter:
    return AudioRouter(blackhole_name="BlackHole 2ch", backend=backend)


class TestEnsureRouted:
    def test_creates_managed_device_and_switches(self, backend):
        router = _router(backend)
        result = router.ensure_routed()

        assert result.changed is True
        assert result.error is None
        assert len(backend.created) == 1
        created = backend.created[0]
        assert created["name"] == MANAGED_DEVICE_NAME
        assert created["uid"] == MANAGED_DEVICE_UID
        assert created["subs"] == [JABRA_UID, BLACKHOLE_UID]
        assert backend.default_output_device() == created["id"]

    def test_noop_when_default_output_is_aggregate_with_blackhole(self, backend):
        backend.devices[9] = {
            "name": "My Multi-Output",
            "uid": "user-multi",
            "subs": [JABRA_UID, BLACKHOLE_UID],
            "output": True,
        }
        backend._default_output = 9
        result = _router(backend).ensure_routed()

        assert result.already_routed is True
        assert result.changed is False
        assert backend.created == []
        assert backend.default_output_device() == 9

    def test_noop_when_default_output_is_blackhole_itself(self, backend):
        backend._default_output = 2
        result = _router(backend).ensure_routed()

        assert result.already_routed is True
        assert backend.created == []

    def test_reuses_existing_managed_device_with_matching_members(self, backend):
        backend.devices[7] = {
            "name": MANAGED_DEVICE_NAME,
            "uid": MANAGED_DEVICE_UID,
            "subs": [JABRA_UID, BLACKHOLE_UID],
            "output": True,
        }
        result = _router(backend).ensure_routed()

        assert result.changed is True
        assert backend.created == []
        assert backend.destroyed == []
        assert backend.default_output_device() == 7

    def test_rebuilds_managed_device_with_stale_members(self, backend):
        # Managed device still points at the speakers, but the user's
        # current output is the Jabra — it must be rebuilt.
        backend.devices[7] = {
            "name": MANAGED_DEVICE_NAME,
            "uid": MANAGED_DEVICE_UID,
            "subs": [SPEAKERS_UID, BLACKHOLE_UID],
            "output": True,
        }
        result = _router(backend).ensure_routed()

        assert result.changed is True
        assert backend.destroyed == [7]
        assert len(backend.created) == 1
        assert backend.created[0]["subs"] == [JABRA_UID, BLACKHOLE_UID]

    def test_user_aggregate_without_blackhole_gets_mirrored(self, backend):
        # Aggregates can't nest: mirror the members and add the loopback.
        backend.devices[9] = {
            "name": "My Multi-Output",
            "uid": "user-multi",
            "subs": [JABRA_UID, SPEAKERS_UID],
            "output": True,
        }
        backend._default_output = 9
        result = _router(backend).ensure_routed()

        assert result.changed is True
        assert backend.created[0]["subs"] == [JABRA_UID, SPEAKERS_UID, BLACKHOLE_UID]

    def test_error_when_blackhole_missing(self, backend):
        del backend.devices[2]
        result = _router(backend).ensure_routed()

        assert result.error is not None
        assert result.changed is False
        assert backend.default_output_device() == 1

    def test_backend_unavailable_is_graceful_noop(self, backend):
        class Unavailable(FakeBackend):
            def available(self) -> bool:
                return False

        router = _router(Unavailable(_standard_devices(), default_output=1))
        result = router.ensure_routed()

        assert isinstance(result, RoutingResult)
        assert result.changed is False
        assert result.error is None

    def test_coreaudio_failure_never_raises(self, backend):
        class Exploding(FakeBackend):
            def default_output_device(self) -> int:
                raise CoreAudioError("HAL says no")

        result = _router(Exploding(_standard_devices(), default_output=1)).ensure_routed()

        assert result.error is not None
        assert result.changed is False

    def test_routing_that_does_not_stick_returns_error(self, backend):
        class NonSticky(FakeBackend):
            def set_default_output_device(self, device_id: int) -> None:
                # Record the request but leave the default unchanged, as if
                # CoreAudio accepted the set without it taking effect.
                self.requested = device_id

        result = _router(NonSticky(_standard_devices(), default_output=1)).ensure_routed()

        assert result.changed is False
        assert result.error is not None
        assert "did not take effect" in result.error.lower()


class TestRestore:
    def test_restore_switches_back_to_previous_output(self, backend):
        router = _router(backend)
        router.ensure_routed()
        assert backend.default_output_device() != 1

        result = router.restore()
        assert result.changed is True
        assert backend.default_output_device() == 1

    def test_restore_without_prior_change_is_noop(self, backend):
        result = _router(backend).restore()
        assert result.changed is False
        assert result.error is None

    def test_restore_respects_user_output_change_mid_meeting(self, backend):
        router = _router(backend)
        router.ensure_routed()
        backend._default_output = 3  # User picked the speakers mid-meeting.

        result = router.restore()
        assert result.changed is False
        assert backend.default_output_device() == 3

    def test_restore_follows_uid_when_device_id_changed(self, backend):
        # USB replug: same UID, new device id.
        router = _router(backend)
        router.ensure_routed()
        backend.devices[42] = backend.devices.pop(1)

        result = router.restore()
        assert result.changed is True
        assert backend.default_output_device() == 42

    def test_second_ensure_after_restore_works(self, backend):
        router = _router(backend)
        router.ensure_routed()
        router.restore()
        result = router.ensure_routed()

        assert result.changed is True
        # Managed device is reused, not recreated.
        assert len(backend.created) == 1
        assert backend.destroyed == []

    def test_restore_heals_stale_hijack_from_previous_process(self):
        """A crash or daemon restart loses the in-memory previous-device
        record while the managed device stays the system default
        (observed live 2026-07-07: a capture failure at 18:21 left
        'Context Recall Audio' as the user's output across two daemon
        restarts, killing their volume keys). A FRESH router must hand
        control back to the first real sub-device instead of declaring
        'nothing to restore'."""
        devices = _standard_devices()
        devices[9] = {
            "name": MANAGED_DEVICE_NAME,
            "uid": MANAGED_DEVICE_UID,
            "subs": [JABRA_UID, BLACKHOLE_UID],
            "output": True,
        }
        backend = FakeBackend(devices, default_output=9)

        result = _router(backend).restore()

        assert result.changed is True
        assert backend.default_output_device() == 1  # the Jabra

    def test_restore_stale_hijack_skips_missing_subdevices(self):
        """If the first real sub-device was unplugged, fall through to
        the next one rather than failing."""
        devices = _standard_devices()
        devices[9] = {
            "name": MANAGED_DEVICE_NAME,
            "uid": MANAGED_DEVICE_UID,
            "subs": ["usb:unplugged-headset", SPEAKERS_UID, BLACKHOLE_UID],
            "output": True,
        }
        backend = FakeBackend(devices, default_output=9)

        result = _router(backend).restore()

        assert result.changed is True
        assert backend.default_output_device() == 3  # the speakers

    def test_restore_stale_hijack_without_real_subdevice_errors(self):
        devices = _standard_devices()
        devices[9] = {
            "name": MANAGED_DEVICE_NAME,
            "uid": MANAGED_DEVICE_UID,
            "subs": [BLACKHOLE_UID],
            "output": True,
        }
        backend = FakeBackend(devices, default_output=9)

        result = _router(backend).restore()

        assert result.changed is False
        assert result.error is not None
        assert backend.default_output_device() == 9  # left alone, not broken
