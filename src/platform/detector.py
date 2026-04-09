"""
Platform detector protocol and factory.

Defines the interface for platform-specific meeting detection and
provides a factory that selects the correct implementation based on
the current operating system.
"""

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformDetector(Protocol):
    """Interface for platform-specific meeting detection."""

    def is_app_running(self, process_names: list[str]) -> bool:
        """Check whether any of the given process names are currently running."""
        ...

    def is_app_using_audio(self, process_names: list[str]) -> bool:
        """Check whether any of the given processes have active audio device handles."""
        ...

    def is_call_window_active(self) -> bool:
        """Fallback heuristic: check window titles for call indicators."""
        ...


def create_detector() -> PlatformDetector:
    """Return the appropriate PlatformDetector for the current OS."""
    if sys.platform == "darwin":
        from src.platform.macos import MacOSDetector

        return MacOSDetector()

    if sys.platform == "linux":
        from src.platform.linux import LinuxDetector

        return LinuxDetector()

    if sys.platform == "win32":
        from src.platform.windows import WindowsDetector

        return WindowsDetector()

    raise NotImplementedError(f"No meeting detector for platform: {sys.platform}")
