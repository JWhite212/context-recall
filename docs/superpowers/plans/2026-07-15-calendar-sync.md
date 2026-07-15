# Calendar Sync (Repair + Picker) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make macOS calendar sync actually work — events populate, the OS permission prompt fires, and recordings can link to calendar events — plus polish the Settings calendar picker.

**Architecture:** The feature is ~80% built (EventKit reader, periodic sync job, mirror table, Settings picker). It is dead due to three stacked bugs: (1) the master gate defaults off, (2) `pyobjc`/EventKit is never bundled into the PyInstaller daemon so the reader silently no-ops in every deployed build, and (3) the daemon Info.plist declares no Calendars usage key, so macOS TCC would kill the daemon on first calendar access rather than prompt. This plan fixes all three, adds an explicit boot-time access request mirroring `src/mic_permission.py`, exposes a permission-status endpoint, and gives the Settings picker a permission banner and a permission-vs-empty distinction. Guard tests lock in the packaging so a future build can't silently regress.

**Tech Stack:** Python 3.12, FastAPI, `pyobjc-framework-eventkit` (already in `requirements.lock`), PyInstaller (`context-recall.spec`), React 19 + TypeScript + TanStack Query + Vitest, pytest.

## Global Constraints

- **macOS + Apple Silicon only.** EventKit access must degrade to a no-op (never raise) when EventKit is unavailable (CI, non-macOS). Every darwin-specific call is guarded.
- **The daemon Info.plist lives ONLY in `context-recall.spec`'s `BUNDLE(info_plist=...)`.** `scripts/build_daemon.sh` does NOT edit the plist (it only repackages + codesigns). Do not add plist edits to the build script.
- **TCC kills a frozen process that requests a permission without the matching usage description.** The Calendars usage keys (Task 2) MUST land before/with any explicit calendar request path (Tasks 3–4).
- **Tests must never trigger a real permission dialog.** Follow the existing pattern in `tests/conftest.py` (mic is forced `authorized`); monkeypatch calendar status/request functions in tests.
- **Conventional-commit messages**, one logical change per commit. Python suite target: `python3 -m pytest tests/ -v` stays green (~1030 tests). UI: `cd ui && npm test` + `npx tsc --noEmit` clean.
- **Recording rules (keyword/domain filters) are OUT OF SCOPE** for this plan.

---

## File Structure

- **Modify** `src/utils/config.py` — flip `CalendarConfig.enabled` default to `True`.
- **Modify** `context-recall.spec` — add EventKit/Foundation/objc hidden imports + `collect_submodules("EventKit")`; add `NSCalendarsUsageDescription` and `NSCalendarsFullAccessUsageDescription` to the BUNDLE `info_plist`.
- **Create** `src/calendar_permission.py` — EventKit TCC introspection + explicit request + boot poller, mirroring `src/mic_permission.py`'s public shape. Pure status mapping is unit-testable; darwin calls are guarded.
- **Modify** `src/main.py` — add `_request_calendar_permission_at_boot()` and spawn it as a daemon thread in `run_daemon()`, mirroring the mic-permission boot thread.
- **Modify** `src/api/routes/calendar.py` — add `GET /api/calendar/permission` returning the process-level calendar TCC status.
- **Modify** `ui/src/lib/api.ts` — add `getCalendarPermission()`.
- **Modify** `ui/src/components/settings/CalendarsSection.tsx` — permission banner + permission-vs-empty state.
- **Create** `tests/test_spec_bundle_guards.py` — guard test asserting EventKit hidden imports + both Calendars plist keys are present in the spec (and the mic key is not lost).
- **Create** `tests/test_calendar_permission.py` — unit tests for the permission module.
- **Modify** `tests/test_config.py` — assert the new `enabled` default.
- **Modify** `tests/test_api_calendar.py` — cover the permission endpoint.
- **Modify** `ui/src/components/settings/__tests__/CalendarsSection.test.tsx` — cover banner + empty states.

---

### Task 1: Flip the calendar master gate default to on

**Files:**

- Modify: `src/utils/config.py:228`
- Test: `tests/test_config.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `CalendarConfig().enabled == True` by default. Callers in `src/api/server.py` (`if _cal_cfg.enabled or _cal_cfg.import_enabled`) and `src/main.py` (`if CalendarMatcher and self._config.calendar.enabled`) now construct the reader/matcher by default; both remain safe because the matcher/reader report `available == False` without EventKit.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_calendar_enabled_defaults_true():
    from src.utils.config import CalendarConfig

    assert CalendarConfig().enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_calendar_enabled_defaults_true -v`
Expected: FAIL — `assert False is True` (current default is `False`).

- [ ] **Step 3: Change the default**

In `src/utils/config.py`, in the `CalendarConfig` dataclass, change:

```python
class CalendarConfig:
    enabled: bool = False
```

to:

```python
class CalendarConfig:
    enabled: bool = True
```

- [ ] **Step 4: Run the test + full config suite**

Run: `python3 -m pytest tests/test_config.py tests/test_config_edge_cases.py -v`
Expected: PASS.

- [ ] **Step 5: Run the orchestrator + server suites (ripple check)**

Run: `python3 -m pytest tests/test_orchestrator.py tests/test_server_calendar_sync.py -v`
Expected: PASS. (The matcher/reader now construct under default config but stay inert because EventKit is absent in tests — `available` is `False`. If any test asserted the matcher is `None` under default config, update it to assert `available is False` instead.)

- [ ] **Step 6: Commit**

```bash
git add src/utils/config.py tests/test_config.py
git commit -m "fix(calendar): enable calendar integration by default"
```

---

### Task 2: Bundle EventKit into the daemon + declare Calendars TCC keys (with guard test)

**Files:**

- Modify: `context-recall.spec:57-104` (hiddenimports) and `context-recall.spec:195-205` (BUNDLE `info_plist`)
- Create: `tests/test_spec_bundle_guards.py`

**Interfaces:**

- Consumes: nothing.
- Produces: a daemon bundle that contains the `EventKit`, `Foundation`, and `objc` Python modules and an Info.plist declaring `NSCalendarsUsageDescription` + `NSCalendarsFullAccessUsageDescription`. The guard test `tests/test_spec_bundle_guards.py` reads the spec as text and fails if any required hidden import or plist key is missing.

**Background:** `src/calendar_events/reader.py` does `import EventKit` and `from Foundation import NSDate`; `src/calendar_matcher.py` does `import EventKit`. None are in the spec's `hiddenimports`, so PyInstaller drops them and the reader is `available == False` in every deployed build. macOS 14+ splits calendar access into full/write-only, so both `NSCalendarsUsageDescription` (legacy) and `NSCalendarsFullAccessUsageDescription` (14+) are required for the prompt to present and for read access.

- [ ] **Step 1: Write the failing guard test**

Create `tests/test_spec_bundle_guards.py`:

```python
"""Guard tests: the PyInstaller spec must bundle the frameworks and declare
the TCC usage keys that deployed features depend on. These features have
silently shipped broken before (voice-ID/speechbrain, sqlite-vec, and the
calendar reader) because a required hidden import or plist key was missing
from the spec. Asserting on the spec text catches that in CI without a build."""

from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "context-recall.spec"


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


@pytest.mark.parametrize("module", ['"EventKit"', '"Foundation"', '"objc"'])
def test_spec_bundles_eventkit_modules(spec_text, module):
    assert module in spec_text, f"{module} missing from context-recall.spec hiddenimports"


def test_spec_collects_eventkit_submodules(spec_text):
    assert 'collect_submodules("EventKit")' in spec_text


@pytest.mark.parametrize(
    "key",
    [
        "NSCalendarsUsageDescription",
        "NSCalendarsFullAccessUsageDescription",
        "NSMicrophoneUsageDescription",  # regression: must not be dropped
    ],
)
def test_spec_declares_tcc_usage_keys(spec_text, key):
    assert key in spec_text, f"{key} missing from context-recall.spec info_plist"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_spec_bundle_guards.py -v`
Expected: FAIL on the EventKit/Foundation/objc and the two `NSCalendars*` assertions (the mic key already passes).

- [ ] **Step 3: Add the EventKit hidden imports**

In `context-recall.spec`, inside the `hiddenimports=[ ... ]` list, add these entries (place them after the `"slugify", "yaml",` output-writers group, before the `torch` group):

```python
        # macOS Calendar integration (EventKit via pyobjc). The reader
        # does `import EventKit` and `from Foundation import NSDate`;
        # neither is discovered by static analysis, so without these the
        # calendar reader ships as available==False in every build.
        "objc",
        "EventKit",
        "Foundation",
        "CoreFoundation",
```

Then, in the same `Analysis(...)` call, add a `collect_submodules("EventKit")` to the concatenated submodule list (the block that already reads `+ collect_submodules("speechbrain")`):

```python
    + collect_submodules("speechbrain")
    + collect_submodules("EventKit"),
```

(Move the trailing comma so `collect_submodules("EventKit")` is the final element of the `hiddenimports=... + ...` expression.)

- [ ] **Step 4: Add the Calendars usage keys to the BUNDLE plist**

In `context-recall.spec`, in the `BUNDLE(...)` call's `info_plist={...}` dict, add both keys after the existing `NSMicrophoneUsageDescription` entry:

```python
        "NSCalendarsUsageDescription": (
            "Context Recall reads your calendar to label recordings with "
            "the matching meeting's title and attendees, and to show your "
            "upcoming meetings. Calendar data stays on this Mac."
        ),
        "NSCalendarsFullAccessUsageDescription": (
            "Context Recall reads your calendar to label recordings with "
            "the matching meeting's title and attendees, and to show your "
            "upcoming meetings. Calendar data stays on this Mac."
        ),
```

- [ ] **Step 5: Run the guard test to verify it passes**

Run: `python3 -m pytest tests/test_spec_bundle_guards.py -v`
Expected: PASS (all parametrized cases).

- [ ] **Step 6: Sanity-check the spec still parses as Python**

Run: `python3 -c "import ast; ast.parse(open('context-recall.spec').read()); print('spec parses')"`
Expected: prints `spec parses` (catches a misplaced comma/bracket from Steps 3–4).

- [ ] **Step 7: Commit**

```bash
git add context-recall.spec tests/test_spec_bundle_guards.py
git commit -m "fix(build): bundle EventKit and declare Calendars TCC keys in the daemon"
```

---

### Task 3: Calendar-permission module (introspection + request + boot poller)

**Files:**

- Create: `src/calendar_permission.py`
- Test: `tests/test_calendar_permission.py`

**Interfaces:**

- Consumes: nothing (EventKit reached lazily + guarded).
- Produces:
  - Constants `AUTHORIZED = "authorized"`, `DENIED = "denied"`, `RESTRICTED = "restricted"`, `NOT_DETERMINED = "not_determined"`, `WRITE_ONLY = "write_only"`, `UNKNOWN = "unknown"`.
  - `authorization_status() -> str` — never prompts; `UNKNOWN` off-darwin or when EventKit is unavailable.
  - `request_access(*, timeout_seconds: float = 15.0) -> bool | None` — fires the OS prompt for THIS process; `None` if unavailable/unanswered.
  - `request_access_at_boot(*, timeout_seconds: float = 300.0, poll_interval: float = 2.0) -> str` — returns early with the current status if already determined; otherwise requests + polls until determined or deadline. Used by `main.py`.
  - `describe_fix(status: str) -> str` — user-facing remediation text.
  - `ensure_calendar_access() -> tuple[str, str | None]` — gate helper: `(status, problem)`, `problem` is `None` when reads may proceed.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_calendar_permission.py`:

```python
"""Tests for src/calendar_permission.py — macOS Calendar (EventKit) TCC.

Mirrors src/mic_permission.py. The pure status mapping is exercised
directly; darwin calls are guarded and return UNKNOWN off-platform. No
test may trigger a real permission dialog."""

import sys

import pytest

from src import calendar_permission as cp
from src.calendar_permission import (
    AUTHORIZED,
    DENIED,
    NOT_DETERMINED,
    RESTRICTED,
    UNKNOWN,
    WRITE_ONLY,
    describe_fix,
    ensure_calendar_access,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, NOT_DETERMINED),
        (1, RESTRICTED),
        (2, DENIED),
        (3, AUTHORIZED),
        (4, WRITE_ONLY),
        (99, UNKNOWN),
    ],
)
def test_status_from_raw(raw, expected):
    assert cp._status_from_raw(raw) == expected


def test_authorization_status_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cp.authorization_status() == UNKNOWN


def test_ensure_calendar_access_authorized_has_no_problem(monkeypatch):
    monkeypatch.setattr(cp, "authorization_status", lambda: AUTHORIZED)
    status, problem = ensure_calendar_access()
    assert status == AUTHORIZED
    assert problem is None


@pytest.mark.parametrize("bad", [DENIED, RESTRICTED, WRITE_ONLY])
def test_ensure_calendar_access_denied_returns_problem(monkeypatch, bad):
    monkeypatch.setattr(cp, "authorization_status", lambda: bad)
    status, problem = ensure_calendar_access()
    assert status == bad
    assert problem is not None
    assert "System Settings" in problem


def test_describe_fix_not_determined_mentions_allow():
    assert "Allow" in describe_fix(NOT_DETERMINED)


def test_request_access_at_boot_returns_early_when_determined(monkeypatch):
    monkeypatch.setattr(cp, "authorization_status", lambda: AUTHORIZED)

    def _boom(**_kwargs):
        raise AssertionError("request_access must not be called when already determined")

    monkeypatch.setattr(cp, "request_access", _boom)
    assert cp.request_access_at_boot() == AUTHORIZED


def test_request_access_at_boot_requests_then_polls(monkeypatch):
    statuses = iter([NOT_DETERMINED, NOT_DETERMINED, AUTHORIZED])
    monkeypatch.setattr(cp, "authorization_status", lambda: next(statuses))
    called = {"n": 0}

    def _req(**_kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr(cp, "request_access", _req)
    monkeypatch.setattr(cp.time, "sleep", lambda *_a: None)
    result = cp.request_access_at_boot(timeout_seconds=100.0, poll_interval=0.0)
    assert result == AUTHORIZED
    assert called["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_calendar_permission.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.calendar_permission'`.

- [ ] **Step 3: Write the module**

Create `src/calendar_permission.py`:

```python
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

        raw = EventKit.EKEventStore.authorizationStatusForEntityType_(
            EventKit.EKEntityTypeEvent
        )
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
        import EventKit

        store = EventKit.EKEventStore.alloc().init()
        done = threading.Event()
        outcome: dict[str, bool] = {}

        def on_access(granted, error):
            outcome["granted"] = bool(granted)
            if error:
                logger.warning("Calendar access error: %s", error)
            done.set()

        # macOS 14+ prefers requestFullAccessToEventsWithCompletion_; the
        # older requestAccessToEntityType_completion_ still functions and
        # keeps one path across OS versions (matches CalendarReader).
        store.requestAccessToEntityType_completion_(
            EventKit.EKEntityTypeEvent, on_access
        )
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


def request_access_at_boot(
    *, timeout_seconds: float = 300.0, poll_interval: float = 2.0
) -> str:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_calendar_permission.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Lint**

Run: `ruff check src/calendar_permission.py tests/test_calendar_permission.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/calendar_permission.py tests/test_calendar_permission.py
git commit -m "feat(calendar): add EventKit TCC permission introspection + request"
```

---

### Task 4: Request calendar permission at daemon boot

**Files:**

- Modify: `src/main.py` (add `_request_calendar_permission_at_boot` near `_request_mic_permission_at_boot` at line 344; spawn thread in `run_daemon` at line 1081)
- Test: `tests/test_calendar_permission.py` (add a boot-helper delegation test)

**Interfaces:**

- Consumes: `src.calendar_permission.request_access_at_boot` (Task 3).
- Produces: `ContextRecall._request_calendar_permission_at_boot(self) -> None`, spawned as a daemon thread in `run_daemon()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_calendar_permission.py`:

```python
def test_boot_helper_delegates_to_module(monkeypatch):
    """main.py's boot helper must call request_access_at_boot and log,
    not reimplement polling."""
    import src.main as main_mod

    calls = {"n": 0}
    monkeypatch.setattr(
        main_mod.calendar_permission,
        "request_access_at_boot",
        lambda **_kw: calls.__setitem__("n", calls["n"] + 1) or AUTHORIZED,
    )
    # Unbound call with a bare namespace as `self` — the helper uses no
    # instance state.
    main_mod.ContextRecall._request_calendar_permission_at_boot(object())
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_calendar_permission.py::test_boot_helper_delegates_to_module -v`
Expected: FAIL — `AttributeError: module 'src.main' has no attribute 'calendar_permission'` (import not added yet) or `has no attribute '_request_calendar_permission_at_boot'`.

- [ ] **Step 3: Import the module in main.py**

In `src/main.py`, next to the existing `from src.mic_permission import ensure_microphone_access` (line 43), add:

```python
from src import calendar_permission
```

- [ ] **Step 4: Add the boot helper**

In `src/main.py`, immediately after the `_request_mic_permission_at_boot` method (ends at line 374), add:

```python
    def _request_calendar_permission_at_boot(self) -> None:
        """Raise the calendar TCC prompt at daemon start when undetermined.

        Runs on its own daemon thread — the dialog can sit unanswered for
        minutes. Delegates the request+poll to calendar_permission so the
        logic stays unit-tested there."""
        status = calendar_permission.request_access_at_boot()
        logger.info("Calendar permission at boot: %s", status)
```

- [ ] **Step 5: Spawn the thread in run_daemon**

In `src/main.py`, in `run_daemon()`, immediately after the existing mic-permission thread block (ends at line 1085), add:

```python
        # Trigger the calendar permission dialog at boot too, on its own
        # thread for the same reason (the dialog can sit unanswered).
        threading.Thread(
            target=self._request_calendar_permission_at_boot,
            name="calendar-permission",
            daemon=True,
        ).start()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_calendar_permission.py::test_boot_helper_delegates_to_module -v`
Expected: PASS.

- [ ] **Step 7: Import-smoke + lint**

Run: `python3 -c "from src.main import ContextRecall"` then `ruff check src/main.py`
Expected: no error, no lint findings.

- [ ] **Step 8: Commit**

```bash
git add src/main.py tests/test_calendar_permission.py
git commit -m "feat(calendar): request calendar permission at daemon boot"
```

---

### Task 5: Expose calendar permission status over the API

**Files:**

- Modify: `src/api/routes/calendar.py` (add endpoint after `get_calendars`, ~line 72)
- Test: `tests/test_api_calendar.py`

**Interfaces:**

- Consumes: `src.calendar_permission.authorization_status` (Task 3).
- Produces: `GET /api/calendar/permission` → `{"status": "<authorized|denied|restricted|not_determined|write_only|unknown>", "granted": <bool>}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_calendar.py` (follow the existing client-fixture style in that file). Minimal self-contained form:

```python
def test_calendar_permission_endpoint(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes import calendar as calendar_routes

    monkeypatch.setattr(
        "src.calendar_permission.authorization_status", lambda: "authorized"
    )
    app = FastAPI()
    app.include_router(calendar_routes.router)
    client = TestClient(app)

    resp = client.get("/api/calendar/permission")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "authorized"
    assert body["granted"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_calendar.py::test_calendar_permission_endpoint -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the endpoint**

In `src/api/routes/calendar.py`, add an import at the top (after `from src.utils.config import load_config`):

```python
from src import calendar_permission
```

Then add, after the `get_calendars` handler (after line 71):

```python
@router.get("/api/calendar/permission", summary="Calendar TCC permission status")
async def get_calendar_permission():
    """Return this process's macOS Calendar permission status for the UI banner."""
    status = calendar_permission.authorization_status()
    return {"status": status, "granted": status == calendar_permission.AUTHORIZED}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_api_calendar.py::test_calendar_permission_endpoint -v`
Expected: PASS.

- [ ] **Step 5: Run the full calendar API suite + lint**

Run: `python3 -m pytest tests/test_api_calendar.py -v && ruff check src/api/routes/calendar.py`
Expected: PASS, no lint findings.

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/calendar.py tests/test_api_calendar.py
git commit -m "feat(calendar): expose GET /api/calendar/permission for the UI"
```

---

### Task 6: Add the UI API client for calendar permission

**Files:**

- Modify: `ui/src/lib/api.ts` (after `triggerCalendarSync`, ~line 667)

**Interfaces:**

- Consumes: `GET /api/calendar/permission` (Task 5).
- Produces: `getCalendarPermission(): Promise<{ status: string; granted: boolean }>`.

- [ ] **Step 1: Add the client function**

In `ui/src/lib/api.ts`, after `triggerCalendarSync` (line 667), add:

```typescript
export async function getCalendarPermission(): Promise<{
  status: string;
  granted: boolean;
}> {
  return request<{ status: string; granted: boolean }>(
    "/api/calendar/permission",
  );
}
```

- [ ] **Step 2: Type-check**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/api.ts
git commit -m "feat(calendar): add getCalendarPermission API client"
```

---

### Task 7: Permission banner + permission-vs-empty state in the picker

**Files:**

- Modify: `ui/src/components/settings/CalendarsSection.tsx`
- Test: `ui/src/components/settings/__tests__/CalendarsSection.test.tsx`

**Interfaces:**

- Consumes: `getCalendarPermission` (Task 6), existing `getCalendars`, `getConfig`, `updateConfig`, `triggerCalendarSync`.
- Produces: the Settings Calendars panel renders (a) a permission banner with an "Open System Settings" affordance when `granted === false`, and (b) an empty-state that says "Calendar access not granted" when unpermissioned vs "No calendars available" when permissioned but empty.

- [ ] **Step 1: Write the failing tests**

In `ui/src/components/settings/__tests__/CalendarsSection.test.tsx`, extend the `fetchMock` to answer the permission route, then add two tests. Inside the existing `beforeEach` `fetchMock`, add this branch (before the final fallback):

```typescript
if (url.includes("/api/calendar/permission")) {
  return new Response(
    JSON.stringify({ status: permissionStatus, granted: permissionGranted }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}
```

At the top of the `describe` block, add mutable state the branch reads:

```typescript
let permissionStatus = "authorized";
let permissionGranted = true;
```

and reset it in `beforeEach`:

```typescript
permissionStatus = "authorized";
permissionGranted = true;
```

Then add:

```typescript
  it("shows a permission banner when calendar access is not granted", async () => {
    permissionStatus = "denied";
    permissionGranted = false;
    render(<CalendarsSection />, { wrapper: makeWrapper() });
    expect(
      await screen.findByText(/calendar access is not granted/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /open system settings/i }),
    ).toBeInTheDocument();
  });

  it("does not show the permission banner when access is granted", async () => {
    render(<CalendarsSection />, { wrapper: makeWrapper() });
    await screen.findByText("Work");
    expect(
      screen.queryByText(/calendar access is not granted/i),
    ).not.toBeInTheDocument();
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ui && npm test -- CalendarsSection`
Expected: FAIL — banner text / button not found.

- [ ] **Step 3: Implement the banner + state**

Replace the body of `ui/src/components/settings/CalendarsSection.tsx` with this (adds the permission query, banner, and permission-vs-empty state; keeps existing include/exclude + sync logic):

```tsx
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getCalendars,
  getCalendarPermission,
  getConfig,
  updateConfig,
  triggerCalendarSync,
} from "../../lib/api";
import { useToast } from "../common/Toast";

/** Settings panel: choose which calendars to import, and sync now. */
export function CalendarsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: permission } = useQuery({
    queryKey: ["calendar-permission"],
    queryFn: getCalendarPermission,
  });
  const { data: calData } = useQuery({
    queryKey: ["calendars"],
    queryFn: getCalendars,
  });
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  const excluded = config?.calendar?.excluded_calendars ?? [];
  const calendars = calData?.calendars ?? [];
  const granted = permission?.granted ?? true;

  const save = useMutation({
    mutationFn: (next: string[]) =>
      updateConfig({ calendar: { excluded_calendars: next } }),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Calendar selection saved.");
    },
    onError: () => toast.error("Failed to save calendar selection."),
  });

  const syncNow = useMutation({
    mutationFn: triggerCalendarSync,
    onSuccess: (r) => toast.success(`Synced ${r.synced} events.`),
    onError: () => toast.error("Sync failed."),
  });

  function toggle(title: string, include: boolean) {
    const next = include
      ? excluded.filter((t) => t !== title)
      : [...excluded, title];
    save.mutate(next);
  }

  function openSystemSettings() {
    window.open(
      "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
      "_blank",
    );
  }

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Calendars</legend>
      <h2 className="text-sm font-medium text-text-primary">Calendars</h2>
      <p className="text-xs text-text-muted mt-1">
        Choose which calendars to import upcoming meetings from.
      </p>

      {!granted && (
        <div className="mt-3 rounded-lg border border-border bg-surface p-3">
          <p className="text-sm text-text-secondary">
            Calendar access is not granted. Context Recall needs macOS Calendar
            permission to import your meetings.
          </p>
          <button
            type="button"
            onClick={openSystemSettings}
            className="mt-2 px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
          >
            Open System Settings
          </button>
        </div>
      )}

      {granted && (
        <div className="py-3 flex flex-col gap-2">
          {calendars.length === 0 ? (
            <p className="text-sm text-text-muted">No calendars available.</p>
          ) : (
            calendars.map((c) => {
              const included = !excluded.includes(c.title);
              return (
                <label
                  key={c.id}
                  className="flex items-center gap-2 text-sm text-text-secondary"
                >
                  <input
                    type="checkbox"
                    checked={included}
                    onChange={(e) => toggle(c.title, e.target.checked)}
                  />
                  {c.title}
                </label>
              );
            })
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending || !granted}
        className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
      >
        Sync now
      </button>
    </fieldset>
  );
}
```

- [ ] **Step 4: Run the component tests to verify they pass**

Run: `cd ui && npm test -- CalendarsSection`
Expected: PASS (new banner tests + the pre-existing tests in the file).

- [ ] **Step 5: Type-check**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/settings/CalendarsSection.tsx ui/src/components/settings/__tests__/CalendarsSection.test.tsx
git commit -m "feat(calendar): permission banner + permission-vs-empty state in picker"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (~1030+ new). If Task 1's default flip broke an orchestrator test that asserted the matcher is `None` under default config, fix that assertion to check `available is False`.

- [ ] **Step 2: Python lint**

Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 3: UI tests + type-check**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Confirm spec still parses**

Run: `python3 -c "import ast; ast.parse(open('context-recall.spec').read()); print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Commit any verification fixes**

```bash
git add -A
git commit -m "test(calendar): fix assertions surfaced by calendar-sync repair"
```

---

## Manual verification (post-deploy, not automatable in CI)

These require a real signed build on the target Mac and are called out because CI cannot cover EventKit:

1. Build via `scripts/build_daemon.sh`; confirm the bundle contains EventKit — `find "dist/context-recall-daemon" -iname "EventKit*" | head` returns a path.
2. Confirm the plist carries the keys — `plutil -p ".../Context Recall Daemon.app/Contents/Info.plist" | grep -i calendar`.
3. Deploy (`launchctl bootout` → `bootstrap`); on first boot the macOS Calendar prompt appears (subject to the known macOS 26.6 beta TCC issue — if it doesn't, the Settings banner's "Open System Settings" path is the fallback).
4. In Settings → Calendars, calendars list populates and "Sync now" reports a non-zero count; the Calendar screen shows upcoming events.

---

## Self-Review

- **Spec coverage:** (1) bundle pyobjc-eventkit → Task 2; (2) NSCalendars usage keys → Task 2 (spec only — build_daemon.sh has no plist block, corrected from the spec's wording); (3) flip `enabled` default → Task 1; `ensure_calendar_access()` + boot request mirroring mic_permission → Tasks 3–4; Settings permission banner + empty-state distinction → Tasks 6–7; bundle/plist guard tests → Task 2; permission endpoint (needed by the banner) → Task 5. Recording rules correctly excluded.
- **Placeholders:** none — every code and test step is concrete.
- **Type consistency:** `authorization_status`, `request_access`, `request_access_at_boot`, `ensure_calendar_access`, and the constants are defined in Task 3 and referenced identically in Tasks 4–5; `getCalendarPermission` defined in Task 6 and consumed in Task 7; endpoint shape `{status, granted}` is consistent across Tasks 5–7.
