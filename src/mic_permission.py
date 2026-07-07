"""
macOS microphone-permission (TCC) introspection and request.

Production evidence (2026-07-07): recordings captured pure digital
silence — RMS exactly -100.0 dBFS from BOTH the loopback and the real
microphone — and after a reboot every ``stream.start()`` failed with
``PaErrorCode -9986``. Both are how CoreAudio manifests a missing
microphone grant. The TCC database still held the user's grant for the
binary's previous path (the app was renamed), and macOS never showed a
new prompt because the daemon only requested access implicitly by
opening input streams.

This module makes the request explicit. ``AVCaptureDevice`` is reached
via ctypes (``objc_msgSend``) so no pyobjc dependency is required, and
every entry point degrades to ``UNKNOWN`` / ``None`` on non-macOS
platforms or binding failure — introspection problems must never block
a recording that might have worked.

Note: capturing the BlackHole loopback is an *input* stream, so
recording system audio alone still requires this permission — a denied
grant breaks recording even with the microphone disabled in settings.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

AUTHORIZED = "authorized"
DENIED = "denied"
RESTRICTED = "restricted"
NOT_DETERMINED = "not_determined"
UNKNOWN = "unknown"

_AV_STATUS = {
    0: NOT_DETERMINED,
    1: RESTRICTED,
    2: DENIED,
    3: AUTHORIZED,
}

_BLOCK_HAS_SIGNATURE = 1 << 30
_BLOCK_IS_GLOBAL = 1 << 28

# Completion blocks handed to AVFoundation may be invoked after our wait
# times out; keep every ctypes object they depend on alive forever.
# Requests are rare (at most a handful per process), so this never grows
# beyond a few entries.
_LIVE_BLOCKS: list[object] = []

_INVOKE_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [
        ("reserved", ctypes.c_ulong),
        ("size", ctypes.c_ulong),
        ("signature", ctypes.c_char_p),
    ]


class _BlockLiteral(ctypes.Structure):
    _fields_ = [
        ("isa", ctypes.c_void_p),
        ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("invoke", ctypes.c_void_p),
        ("descriptor", ctypes.c_void_p),
    ]


class _AVBinding:
    """Lazily-resolved AVFoundation/objc handles. None fields = unusable."""

    _instance: "_AVBinding | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self.objc = None
        self.av_capture_device = None
        self.media_type_audio = None
        self.global_block_isa = None
        if sys.platform != "darwin":
            return
        try:
            objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
            avf = ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]

            cls = objc.objc_getClass(b"AVCaptureDevice")
            media = ctypes.c_void_p.in_dll(avf, "AVMediaTypeAudio").value
            block_isa = ctypes.addressof(
                (ctypes.c_void_p * 32).in_dll(ctypes.CDLL(None), "_NSConcreteGlobalBlock")
            )
            if not cls or not media:
                raise OSError("AVCaptureDevice / AVMediaTypeAudio not resolved")

            self.objc = objc
            self.av_capture_device = cls
            self.media_type_audio = media
            self.global_block_isa = block_isa
        except Exception:
            logger.debug("AVFoundation binding unavailable", exc_info=True)
            self.objc = None

    @property
    def usable(self) -> bool:
        return self.objc is not None

    @classmethod
    def shared(cls) -> "_AVBinding":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


def _authorization_status_darwin() -> str:
    """Read AVCaptureDevice.authorizationStatus(for: .audio). Never prompts."""
    binding = _AVBinding.shared()
    if not binding.usable:
        return UNKNOWN
    try:
        send = ctypes.cast(
            binding.objc.objc_msgSend,
            ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p),
        )
        sel = binding.objc.sel_registerName(b"authorizationStatusForMediaType:")
        raw = send(binding.av_capture_device, sel, binding.media_type_audio)
        return _AV_STATUS.get(int(raw), UNKNOWN)
    except Exception:
        logger.debug("authorizationStatusForMediaType failed", exc_info=True)
        return UNKNOWN


def _request_access_darwin(timeout_seconds: float) -> bool | None:
    """Fire the system microphone prompt and wait for the user's answer.

    Returns True/False for granted/denied, or None when the dialog was
    not answered within the timeout (it stays on screen — a later
    ``authorization_status()`` call observes the eventual answer) or the
    binding failed.
    """
    binding = _AVBinding.shared()
    if not binding.usable:
        return None
    try:
        done = threading.Event()
        outcome: dict[str, bool] = {}

        def _completion(_block: int, granted: bool) -> None:
            outcome["granted"] = bool(granted)
            done.set()

        invoke = _INVOKE_FUNC(_completion)
        descriptor = _BlockDescriptor(
            reserved=0,
            size=ctypes.sizeof(_BlockLiteral),
            signature=b"v16@?0B8",
        )
        block = _BlockLiteral(
            isa=binding.global_block_isa,
            flags=_BLOCK_HAS_SIGNATURE | _BLOCK_IS_GLOBAL,
            reserved=0,
            invoke=ctypes.cast(invoke, ctypes.c_void_p),
            descriptor=ctypes.cast(ctypes.pointer(descriptor), ctypes.c_void_p),
        )
        _LIVE_BLOCKS.append((invoke, descriptor, block, _completion))

        send = ctypes.cast(
            binding.objc.objc_msgSend,
            ctypes.CFUNCTYPE(
                None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            ),
        )
        sel = binding.objc.sel_registerName(b"requestAccessForMediaType:completionHandler:")
        send(
            binding.av_capture_device,
            sel,
            binding.media_type_audio,
            ctypes.cast(ctypes.byref(block), ctypes.c_void_p),
        )

        if not done.wait(timeout=timeout_seconds):
            logger.info(
                "Microphone permission dialog not answered within %.0fs — "
                "it stays on screen; recording can be retried after answering.",
                timeout_seconds,
            )
            return None
        return outcome.get("granted")
    except Exception:
        logger.debug("requestAccessForMediaType failed", exc_info=True)
        return None


def _has_mic_usage_description() -> bool:
    """Whether the main bundle declares NSMicrophoneUsageDescription."""
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFBundleGetMainBundle.restype = ctypes.c_void_p
        bundle = cf.CFBundleGetMainBundle()
        if not bundle:
            return False
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        key = cf.CFStringCreateWithCString(None, b"NSMicrophoneUsageDescription", 0x08000100)
        cf.CFBundleGetValueForInfoDictionaryKey.restype = ctypes.c_void_p
        cf.CFBundleGetValueForInfoDictionaryKey.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        value = cf.CFBundleGetValueForInfoDictionaryKey(bundle, key)
        if key:
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            cf.CFRelease(key)
        return bool(value)
    except Exception:
        logger.debug("usage-description check failed", exc_info=True)
        return False


def authorization_status() -> str:
    """Current microphone TCC status for THIS process. Never prompts."""
    if sys.platform != "darwin":
        return UNKNOWN
    return _authorization_status_darwin()


def request_access(*, timeout_seconds: float = 15.0) -> bool | None:
    """Trigger the macOS microphone permission dialog for THIS process."""
    if sys.platform != "darwin":
        return None
    if getattr(sys, "frozen", False) and not _has_mic_usage_description():
        # TCC KILLS a process that requests access without a usage
        # description (observed 2026-07-07: launchd crash loop,
        # OS_REASON_TCC). A dev run under a terminal is covered by the
        # terminal's own description; the frozen daemon must ship inside
        # its app-bundle wrapper (scripts/build_daemon.sh).
        logger.error(
            "Refusing to request microphone access: this frozen binary "
            "carries no NSMicrophoneUsageDescription, and macOS would "
            "kill the process. Rebuild with scripts/build_daemon.sh so "
            "the daemon ships inside its app-bundle wrapper."
        )
        return None
    return _request_access_darwin(timeout_seconds)


def describe_fix(status: str) -> str:
    """Actionable, user-facing explanation for a non-authorized status."""
    if status == NOT_DETERMINED:
        return (
            "macOS is asking for microphone access — click Allow on the "
            "permission dialog, then start the recording again."
        )
    return (
        "Microphone access is denied for the Context Recall daemon "
        "(recording system audio through BlackHole needs it too, not just "
        "the mic). Open System Settings → Privacy & Security → Microphone "
        "and enable 'context-recall-daemon', then try again."
    )


def trigger_prompt_via_input_probe(seconds: float = 0.6) -> None:
    """Raise the standard microphone prompt by briefly opening an input.

    tccd kills a launchd daemon that calls AVCaptureDevice
    requestAccessForMediaType even when the bundle carries the usage
    description (observed 2026-07-07: OS_REASON_TCC crash loop with the
    key demonstrably sealed into the bundle). The IMPLICIT request —
    simply opening an input stream — has never killed this daemon in
    months of production, and with the usage description present it
    raises the normal permission dialog. Best-effort: every failure is
    swallowed (a failed probe just means no prompt yet).
    """
    try:
        import sounddevice as sd

        stream = sd.InputStream(channels=1, blocksize=1024)
        try:
            stream.start()
            time.sleep(seconds)
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
    except Exception:
        logger.debug("input-probe prompt trigger failed", exc_info=True)


def ensure_microphone_access() -> tuple[str, str | None]:
    """Gate helper for recording starts.

    Returns ``(status, problem)``. ``problem`` is None when recording may
    proceed and a user-facing message otherwise. NOT_DETERMINED proceeds:
    opening the capture streams performs the implicit TCC request, which
    shows the system dialog now that the daemon bundle carries a usage
    description. Only an explicit denial blocks the start. UNKNOWN also
    proceeds — the silent-input detector remains the runtime backstop.
    """
    status = authorization_status()
    if status in (DENIED, RESTRICTED):
        return status, describe_fix(status)
    return status, None
