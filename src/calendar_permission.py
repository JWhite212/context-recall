"""
macOS Calendar-permission (TCC) introspection and request via EventKit.

Mirrors src/mic_permission.py. EventKit is now bundled into the daemon
(context-recall.spec), so unlike the microphone case we can use the
pyobjc bindings directly instead of a ctypes dance. Every entry point
degrades to UNKNOWN / None off-darwin or when EventKit is unavailable
(CI, missing framework) — an introspection failure must never block a
read that might have worked.

macOS 14 split calendar access into full-access (read) and write-only.
Raw EKAuthorizationStatus values: 0 notDetermined, 1 restricted,
2 denied, 3 authorized/fullAccess, 4 writeOnly. write-only cannot read
events, so we treat it as a blocking state for our read use.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

AUTHORIZED = "authorized"
DENIED = "denied"
RESTRICTED = "restricted"
NOT_DETERMINED = "not_determined"
WRITE_ONLY = "write_only"
UNKNOWN = "unknown"

_EK_STATUS = {
    0: NOT_DETERMINED,
    1: RESTRICTED,
    2: DENIED,
    3: AUTHORIZED,
    4: WRITE_ONLY,
}

# States from which calendar events cannot be read.
_BLOCKING = {DENIED, RESTRICTED, WRITE_ONLY}

# Process-wide singleton EKEventStore. macOS caps the number of EKEventStore
# instances a process may hold and returns EKCADErrorDomain 1021 ("too many
# EKEventStore instances") once exceeded — after which EVERY EventKit call,
# including authorizationStatusForEntityType_, starts failing and the calendar
# grant can never finalise. Allocating a fresh store per request/reader/matcher
# is exactly what triggers that, so every store consumer shares this one.
_shared_store = None
_shared_store_lock = threading.Lock()


def get_shared_store():
    """Return the process-wide EKEventStore, creating it once. None when
    EventKit is unavailable or store creation fails. Thread-safe."""
    global _shared_store
    if not _eventkit_available():
        return None
    with _shared_store_lock:
        if _shared_store is None:
            try:
                import EventKit

                _shared_store = EventKit.EKEventStore.alloc().init()
            except Exception:
                logger.debug("EKEventStore alloc failed", exc_info=True)
                return None
        return _shared_store


def reset_shared_store() -> None:
    """Drop the cached store. For tests (which inject fake EventKit modules)
    and defensive re-init; production never needs it."""
    global _shared_store
    with _shared_store_lock:
        _shared_store = None


def _status_from_raw(raw: int) -> str:
    """Map an EKAuthorizationStatus int to our string. Pure/testable."""
    return _EK_STATUS.get(int(raw), UNKNOWN)


def _eventkit_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import EventKit  # noqa: F401

        return True
    except Exception:
        logger.debug("EventKit unavailable", exc_info=True)
        return False


def authorization_status() -> str:
    """Current calendar TCC status for THIS process. Never prompts."""
    if not _eventkit_available():
        return UNKNOWN
    try:
        import EventKit

        raw = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
        return _status_from_raw(raw)
    except Exception:
        logger.debug("authorizationStatusForEntityType failed", exc_info=True)
        return UNKNOWN


def request_access(*, timeout_seconds: float = 15.0) -> bool | None:
    """Fire the macOS calendar permission dialog for THIS process.

    Returns True/False for granted/denied, or None when unavailable or
    the dialog was not answered within the timeout (it stays on screen;
    a later authorization_status() observes the eventual answer).

    Unlike the microphone case, requesting EventKit access does not kill
    the launchd daemon — the CalendarReader already uses this same
    request path in production. It is still guarded so a mis-built bundle
    lacking the Calendars usage key degrades to None instead of crashing.
    """
    if not _eventkit_available():
        return None
    try:
        store = get_shared_store()
        if store is None:
            return None
        done = threading.Event()
        outcome: dict[str, bool] = {}

        def on_access(granted, error):
            outcome["granted"] = bool(granted)
            if error:
                logger.warning("Calendar access error: %s", error)
            done.set()

        # macOS 14 split calendar access into full-access and write-only;
        # only the full-access request reliably grants read access there.
        # Prefer the modern API when the store exposes it and fall back to
        # the legacy entity-type request on older systems.
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(on_access)
        else:
            import EventKit

            store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, on_access)
        if done.wait(timeout=timeout_seconds):
            return outcome.get("granted")
        logger.info(
            "Calendar permission dialog not answered within %.0fs — it stays "
            "on screen; a later status check observes the answer.",
            timeout_seconds,
        )
        return None
    except Exception:
        logger.debug("requestAccessToEntityType failed", exc_info=True)
        return None


def request_access_at_boot(*, timeout_seconds: float = 300.0, poll_interval: float = 2.0) -> str:
    """Raise the prompt at daemon start when still undetermined, then poll
    for the user's answer so the boot log records the outcome. Returns the
    final observed status."""
    status = authorization_status()
    if status != NOT_DETERMINED:
        return status
    logger.info("Calendar permission undetermined — raising the system dialog.")
    request_access(timeout_seconds=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = authorization_status()
        if status != NOT_DETERMINED:
            break
        time.sleep(poll_interval)
    return status


def describe_fix(status: str) -> str:
    """Actionable, user-facing explanation for a non-authorized status."""
    if status == NOT_DETERMINED:
        return (
            "macOS is asking for calendar access — click Allow on the "
            "permission dialog, then reopen the Calendars settings."
        )
    return (
        "Calendar access is denied for the Context Recall daemon. Open "
        "System Settings → Privacy & Security → Calendars and enable "
        "'context-recall-daemon', then try again."
    )


def ensure_calendar_access() -> tuple[str, str | None]:
    """Gate helper. Returns (status, problem); problem is None when reads
    may proceed. UNKNOWN proceeds — the reader's own guard is the backstop."""
    status = authorization_status()
    if status in _BLOCKING:
        return status, describe_fix(status)
    return status, None
