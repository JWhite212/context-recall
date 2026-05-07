"""
Centralised data paths for Context Recall.

All persistent data lives under macOS-native locations. This helper
exists so individual modules don't duplicate path construction, and so
dev / prod / test profiles can be isolated from each other.

Profiles
========

The active profile is selected by the `CONTEXT_RECALL_PROFILE` environment
variable. Recognised values:

  prod (default)  Real user data. Lives under
                  ~/Library/Application Support/Context Recall, etc.

  dev             Developer profile. Lives under
                  ~/Library/Application Support/Context Recall Dev, etc.
                  `make dev-daemon` and `make reset-dev` use this.

  test            Process-local temp directory rooted under
                  $TMPDIR. Suitable for the test suite.

Setting `CONTEXT_RECALL_HOME=/some/path` overrides every path so the entire
data tree lives under that single root. Useful for one-off automation,
ephemeral CI runs, or pointing the daemon at an external volume. The
override applies regardless of profile.

Production data is never touched when the dev or test profiles are
active, so running tests, manual recordings, or experiments cannot
pollute real meeting history, audio, or auth tokens.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PROD_APP_NAME = "Context Recall"
DEV_APP_NAME = "Context Recall Dev"

VALID_PROFILES = ("prod", "dev", "test")


def profile_name() -> str:
    """Return the active profile, defaulting to ``prod``.

    Unknown values fall back to ``prod`` so a typo in the environment
    cannot accidentally point the daemon at an unintended data tree.
    """
    raw = os.environ.get("CONTEXT_RECALL_PROFILE", "prod").strip().lower()
    return raw if raw in VALID_PROFILES else "prod"


def app_name() -> str:
    """Macro-segregated app name. Dev uses a separate folder."""
    return DEV_APP_NAME if profile_name() == "dev" else PROD_APP_NAME


def _override_root() -> Path | None:
    """Honour ``CONTEXT_RECALL_HOME`` if set."""
    raw = os.environ.get("CONTEXT_RECALL_HOME")
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def _profile_root() -> Path:
    """Compose the per-profile root directory.

    For ``prod`` and ``dev`` this is ``~/Library`` (with the app name
    handling the dev / prod split). For ``test`` it's a process-stable
    temp dir so the suite can run in isolation. ``CONTEXT_RECALL_HOME``
    short-circuits both.
    """
    override = _override_root()
    if override is not None:
        return override
    if profile_name() == "test":
        return Path(tempfile.gettempdir()) / "context-recall-test"
    return Path(os.path.expanduser("~"))


def _section(library_subdir: str) -> Path:
    """Build a ``<root>/<library_subdir>/<app>`` path.

    For prod/dev (real macOS root) ``library_subdir`` is one of
    ``Library/Application Support``, ``Library/Caches``, ``Library/Logs``.
    For test or override roots we use the same layout so paths are
    predictable across profiles.
    """
    root = _profile_root()
    return root / library_subdir / app_name()


def app_support_dir() -> Path:
    return _section("Library/Application Support")


def cache_dir() -> Path:
    return _section("Library/Caches")


def logs_dir() -> Path:
    return _section("Library/Logs")


def db_path() -> Path:
    return app_support_dir() / "meetings.db"


def audio_dir() -> Path:
    return app_support_dir() / "audio"


def auth_token_path() -> Path:
    return app_support_dir() / "auth_token"


def templates_dir() -> Path:
    return app_support_dir() / "templates"


def default_log_file() -> Path:
    return logs_dir() / "contextrecall.log"
