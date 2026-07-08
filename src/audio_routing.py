"""
Automatic system-audio routing (macOS CoreAudio).

BlackHole only receives system audio when the default output device
feeds it — historically the user had to hand-build a Multi-Output
Device in Audio MIDI Setup and remember to switch to it, and recordings
were silent whenever they forgot (or built it without BlackHole in it).

AudioRouter removes that friction: at recording start it checks whether
the current default output already routes into the loopback, and if not
it finds-or-creates a managed Multi-Output Device ("Context Recall
Audio" = current output + BlackHole), switches the default output to it,
and remembers the previous device. After the recording it switches back
— unless the user changed outputs mid-meeting, in which case their
choice wins.

The CoreAudio calls go through ``CoreAudioBackend`` (ctypes, no extra
dependencies) so the routing logic is testable with a fake backend.
All failures downgrade to a ``RoutingResult`` with ``error`` set —
recording proceeds regardless, at worst with the old behaviour.
"""

from __future__ import annotations

import ctypes
import logging
import plistlib
import sys
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MANAGED_DEVICE_NAME = "Context Recall Audio"
MANAGED_DEVICE_UID = "dev.contextrecall.managed-multi-output"

# CoreAudio can take a moment to propagate a default-output change, so the
# routing verification polls briefly before declaring failure (Bug #5).
_ROUTING_VERIFY_ATTEMPTS = 5
_ROUTING_VERIFY_INTERVAL_SECONDS = 0.05


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDefaultOutputDevice = _fourcc("dOut")
_kAudioHardwarePropertyDevices = _fourcc("dev#")
_kAudioDevicePropertyDeviceUID = _fourcc("uid ")
_kAudioObjectPropertyName = _fourcc("lnam")
_kAudioAggregateDevicePropertyFullSubDeviceList = _fourcc("grup")
_kAudioDevicePropertyStreams = _fourcc("stm#")
_kAudioObjectPropertyScopeGlobal = _fourcc("glob")
_kAudioObjectPropertyScopeOutput = _fourcc("outp")
_kAudioObjectPropertyElementMain = 0
_kCFStringEncodingUTF8 = 0x08000100


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _addr(selector: int, scope: int = _kAudioObjectPropertyScopeGlobal):
    return _AudioObjectPropertyAddress(selector, scope, _kAudioObjectPropertyElementMain)


class CoreAudioError(OSError):
    """A CoreAudio call returned a non-zero OSStatus."""


class CoreAudioBackend:
    """Thin ctypes wrapper over the CoreAudio HAL. No routing logic here."""

    def __init__(self) -> None:
        self._ca = None
        self._cf = None
        if sys.platform == "darwin":
            try:
                self._ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
                self._cf = ctypes.CDLL(
                    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
                )
            except OSError:
                self._ca = None
                self._cf = None

    def available(self) -> bool:
        return self._ca is not None and self._cf is not None

    # -- property plumbing ------------------------------------------------

    def _get_prop(self, obj_id: int, address, buf) -> int:
        size = ctypes.c_uint32(ctypes.sizeof(buf))
        status = self._ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj_id),
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(buf),
        )
        if status != 0:
            raise CoreAudioError(f"AudioObjectGetPropertyData failed: {status}")
        return size.value

    def _get_prop_size(self, obj_id: int, address) -> int:
        size = ctypes.c_uint32(0)
        status = self._ca.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(obj_id), ctypes.byref(address), 0, None, ctypes.byref(size)
        )
        if status != 0:
            raise CoreAudioError(f"AudioObjectGetPropertyDataSize failed: {status}")
        return size.value

    def _cfstring_to_str(self, cfstr) -> str:
        cf = self._cf
        cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ptr = cf.CFStringGetCStringPtr(cfstr, _kCFStringEncodingUTF8)
        if ptr:
            return ptr.decode("utf-8")
        buf = ctypes.create_string_buffer(1024)
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        if not cf.CFStringGetCString(cfstr, buf, 1024, _kCFStringEncodingUTF8):
            raise CoreAudioError("CFStringGetCString failed")
        return buf.value.decode("utf-8")

    def _device_cfstring_prop(self, device_id: int, selector: int) -> str:
        cfstr = ctypes.c_void_p(0)
        self._get_prop(device_id, _addr(selector), cfstr)
        try:
            return self._cfstring_to_str(cfstr)
        finally:
            self._cf.CFRelease.argtypes = [ctypes.c_void_p]
            if cfstr.value:
                self._cf.CFRelease(cfstr)

    # -- public primitives -------------------------------------------------

    def default_output_device(self) -> int:
        dev = ctypes.c_uint32(0)
        self._get_prop(
            _kAudioObjectSystemObject,
            _addr(_kAudioHardwarePropertyDefaultOutputDevice),
            dev,
        )
        return dev.value

    def set_default_output_device(self, device_id: int) -> None:
        address = _addr(_kAudioHardwarePropertyDefaultOutputDevice)
        val = ctypes.c_uint32(device_id)
        status = self._ca.AudioObjectSetPropertyData(
            _kAudioObjectSystemObject, ctypes.byref(address), 0, None, 4, ctypes.byref(val)
        )
        if status != 0:
            raise CoreAudioError(f"set default output failed: {status}")

    def device_name(self, device_id: int) -> str:
        return self._device_cfstring_prop(device_id, _kAudioObjectPropertyName)

    def device_uid(self, device_id: int) -> str:
        return self._device_cfstring_prop(device_id, _kAudioDevicePropertyDeviceUID)

    def all_device_ids(self) -> list[int]:
        address = _addr(_kAudioHardwarePropertyDevices)
        size = self._get_prop_size(_kAudioObjectSystemObject, address)
        count = max(size // 4, 1)
        buf = (ctypes.c_uint32 * count)()
        actual = ctypes.c_uint32(size)
        status = self._ca.AudioObjectGetPropertyData(
            _kAudioObjectSystemObject,
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(actual),
            ctypes.byref(buf),
        )
        if status != 0:
            raise CoreAudioError(f"device enumeration failed: {status}")
        return list(buf[: actual.value // 4])

    def has_output_streams(self, device_id: int) -> bool:
        address = _addr(_kAudioDevicePropertyStreams, _kAudioObjectPropertyScopeOutput)
        try:
            return self._get_prop_size(device_id, address) > 0
        except CoreAudioError:
            return False

    def subdevice_uids(self, device_id: int) -> list[str] | None:
        """Sub-device UIDs for aggregate/multi-output devices, else None."""
        address = _addr(_kAudioAggregateDevicePropertyFullSubDeviceList)
        arr = ctypes.c_void_p(0)
        try:
            self._get_prop(device_id, address, arr)
        except CoreAudioError:
            return None
        if not arr.value:
            return None
        cf = self._cf
        try:
            cf.CFArrayGetCount.restype = ctypes.c_long
            cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
            cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
            cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
            count = cf.CFArrayGetCount(arr)
            return [self._cfstring_to_str(cf.CFArrayGetValueAtIndex(arr, i)) for i in range(count)]
        finally:
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            cf.CFRelease(arr)

    def create_multi_output(
        self, name: str, uid: str, subdevice_uids: list[str], master_uid: str
    ) -> int:
        """Create a stacked aggregate (Multi-Output Device). Returns its id."""
        description = {
            "name": name,
            "uid": uid,
            "subdevices": [{"uid": sub} for sub in subdevice_uids],
            "master": master_uid,
            "stacked": 1,
            "private": 0,
        }
        data = plistlib.dumps(description, fmt=plistlib.FMT_BINARY)

        cf = self._cf
        cf.CFDataCreate.restype = ctypes.c_void_p
        cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long]
        cfdata = cf.CFDataCreate(None, data, len(data))
        if not cfdata:
            raise CoreAudioError("CFDataCreate failed")

        cf.CFPropertyListCreateWithData.restype = ctypes.c_void_p
        cf.CFPropertyListCreateWithData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        plist = cf.CFPropertyListCreateWithData(None, cfdata, 0, None, None)
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease(cfdata)
        if not plist:
            raise CoreAudioError("CFPropertyListCreateWithData failed")

        dev_id = ctypes.c_uint32(0)
        self._ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
        self._ca.AudioHardwareCreateAggregateDevice.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        status = self._ca.AudioHardwareCreateAggregateDevice(plist, ctypes.byref(dev_id))
        cf.CFRelease(plist)
        if status != 0:
            raise CoreAudioError(f"AudioHardwareCreateAggregateDevice failed: {status}")
        return dev_id.value

    def destroy_aggregate(self, device_id: int) -> None:
        self._ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int32
        self._ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        status = self._ca.AudioHardwareDestroyAggregateDevice(device_id)
        if status != 0:
            raise CoreAudioError(f"AudioHardwareDestroyAggregateDevice failed: {status}")


@dataclass
class RoutingResult:
    """Outcome of an ensure_routed()/restore() call."""

    changed: bool = False
    already_routed: bool = False
    message: str = ""
    error: str | None = None


class AudioRouter:
    """Ensures the default output feeds the capture loopback while recording."""

    def __init__(self, blackhole_name: str = "BlackHole", backend: CoreAudioBackend | None = None):
        self._blackhole_name = blackhole_name or "BlackHole"
        self._backend = backend if backend is not None else CoreAudioBackend()
        self._lock = threading.Lock()
        # Device we switched away from; None when we haven't changed routing.
        self._previous_output_id: int | None = None
        self._previous_output_uid: str | None = None
        self._managed_id: int | None = None

    # -- helpers -----------------------------------------------------------

    def _find_blackhole_uid(self) -> str | None:
        needle = self._blackhole_name.lower()
        fallback = None
        for dev_id in self._backend.all_device_ids():
            try:
                name = self._backend.device_name(dev_id).lower()
            except CoreAudioError:
                continue
            if not self._backend.has_output_streams(dev_id):
                continue
            if needle in name:
                return self._backend.device_uid(dev_id)
            if "blackhole" in name and fallback is None:
                fallback = self._backend.device_uid(dev_id)
        return fallback

    def _find_device_by_uid(self, uid: str) -> int | None:
        for dev_id in self._backend.all_device_ids():
            try:
                if self._backend.device_uid(dev_id) == uid:
                    return dev_id
            except CoreAudioError:
                continue
        return None

    # -- public API ----------------------------------------------------------

    def ensure_routed(self) -> RoutingResult:
        """Route the default output into the loopback if it isn't already.

        Never raises: failures come back as RoutingResult.error so the
        recording can proceed (mic capture still works without routing).
        """
        with self._lock:
            try:
                return self._ensure_routed_locked()
            except CoreAudioError as e:
                logger.warning("Audio routing failed: %s", e)
                return RoutingResult(error=f"Automatic audio routing failed: {e}")

    def _ensure_routed_locked(self) -> RoutingResult:
        if not self._backend.available():
            return RoutingResult(message="CoreAudio unavailable — skipping auto-routing.")

        blackhole_uid = self._find_blackhole_uid()
        if blackhole_uid is None:
            return RoutingResult(
                error=(
                    "BlackHole output device not found — cannot route system audio automatically."
                )
            )

        current_id = self._backend.default_output_device()
        current_uid = self._backend.device_uid(current_id)
        current_subs = self._backend.subdevice_uids(current_id)

        if current_uid == blackhole_uid or (current_subs and blackhole_uid in current_subs):
            return RoutingResult(
                already_routed=True,
                message="System output already feeds the capture device.",
            )

        # Compose the managed device's members from the real output(s).
        if current_uid == MANAGED_DEVICE_UID:
            # Leftover from a crash: the managed device is default but no
            # longer contains BlackHole (shouldn't happen) — rebuild below.
            real_uids = [u for u in (current_subs or []) if u != blackhole_uid]
        elif current_subs:
            # The default is a user aggregate without BlackHole. Aggregates
            # cannot nest, so mirror its members and add the loopback.
            real_uids = [u for u in current_subs if u != blackhole_uid]
        else:
            real_uids = [current_uid]

        if not real_uids:
            return RoutingResult(
                error="No real output device found to pair with the capture device."
            )

        desired_subs = real_uids + [blackhole_uid]

        managed_id = self._find_device_by_uid(MANAGED_DEVICE_UID)
        if managed_id is not None:
            existing_subs = self._backend.subdevice_uids(managed_id) or []
            if set(existing_subs) != set(desired_subs):
                # Stale membership (the user's output changed since last
                # time) — rebuild with the current output.
                self._backend.destroy_aggregate(managed_id)
                managed_id = None

        if managed_id is None:
            managed_id = self._backend.create_multi_output(
                MANAGED_DEVICE_NAME,
                MANAGED_DEVICE_UID,
                desired_subs,
                master_uid=real_uids[0],
            )

        current_name = self._backend.device_name(current_id)
        self._backend.set_default_output_device(managed_id)
        # Verify the switch actually took effect. CoreAudio can accept the set
        # and take a moment to propagate the new default, so poll briefly
        # before declaring failure — a silent no-op would capture only the
        # microphone (Bug #5). Keep the previous-output bookkeeping on the
        # confirmed-success path so restore() never reverts a switch that
        # never happened.
        engaged = False
        for attempt in range(_ROUTING_VERIFY_ATTEMPTS):
            if self._backend.default_output_device() == managed_id:
                engaged = True
                break
            if attempt < _ROUTING_VERIFY_ATTEMPTS - 1:
                time.sleep(_ROUTING_VERIFY_INTERVAL_SECONDS)
        if not engaged:
            return RoutingResult(
                error=(
                    "System audio routing did not take effect — the default "
                    f"output is still '{current_name}'; system audio will not "
                    "be captured. Route output to a Multi-Output Device "
                    "containing BlackHole in Audio MIDI Setup."
                )
            )
        self._previous_output_id = current_id
        self._previous_output_uid = current_uid
        self._managed_id = managed_id

        message = (
            f"System audio routed through '{MANAGED_DEVICE_NAME}' "
            f"({current_name} + capture) for this recording."
        )
        logger.info(message)
        return RoutingResult(changed=True, message=message)

    def restore(self) -> RoutingResult:
        """Switch the default output back to the pre-recording device.

        Respects the user: if they changed the default output mid-meeting
        to something other than our managed device, leave it alone. The
        managed device itself is kept for reuse (destroying it while apps
        hold it open causes glitches; it is rebuilt on demand anyway).
        """
        with self._lock:
            if self._previous_output_id is None:
                return self._heal_stale_hijack_locked()
            try:
                current_id = self._backend.default_output_device()
                if self._managed_id is not None and current_id != self._managed_id:
                    return RoutingResult(
                        message="Output device changed during recording — leaving as-is."
                    )
                target = self._previous_output_id
                # Device ids are not stable across unplug/replug; fall back
                # to the UID if the old id vanished.
                if self._previous_output_uid is not None:
                    by_uid = self._find_device_by_uid(self._previous_output_uid)
                    if by_uid is not None:
                        target = by_uid
                self._backend.set_default_output_device(target)
                message = "Restored previous output device after recording."
                logger.info(message)
                return RoutingResult(changed=True, message=message)
            except CoreAudioError as e:
                logger.warning("Failed to restore output device: %s", e)
                return RoutingResult(error=f"Failed to restore output device: {e}")
            finally:
                self._previous_output_id = None
                self._previous_output_uid = None

    def _heal_stale_hijack_locked(self) -> RoutingResult:
        """Recover when the managed device is default but this process
        never switched to it.

        A crash, a capture-thread failure, or a daemon restart loses the
        in-memory previous-device record while the managed multi-output
        stays the system default — the user is left without volume keys
        until something switches away. Hand control back to the first
        sub-device that still exists and isn't the loopback.
        """
        if not self._backend.available():
            return RoutingResult(message="Routing unchanged — nothing to restore.")
        try:
            current_id = self._backend.default_output_device()
            if self._backend.device_uid(current_id) != MANAGED_DEVICE_UID:
                return RoutingResult(message="Routing unchanged — nothing to restore.")

            subs = self._backend.subdevice_uids(current_id) or []
            blackhole_uid = self._find_blackhole_uid()
            real_uids = [u for u in subs if u != blackhole_uid and "blackhole" not in u.lower()]
            for uid in real_uids:
                target = self._find_device_by_uid(uid)
                if target is None:
                    continue
                self._backend.set_default_output_device(target)
                message = (
                    "Recovered the default output from a stale managed device "
                    f"(switched to '{self._backend.device_name(target)}')."
                )
                logger.info(message)
                return RoutingResult(changed=True, message=message)
            return RoutingResult(
                error=(
                    "The managed output device is the system default but none "
                    "of its sub-devices could be restored — pick an output in "
                    "System Settings → Sound."
                )
            )
        except CoreAudioError as e:
            logger.warning("Stale-routing recovery failed: %s", e)
            return RoutingResult(error=f"Failed to restore output device: {e}")
