"""
Windows meeting detection stub.

Not yet implemented — placeholder for future cross-platform support.
"""


class WindowsDetector:
    """Stub detector for Windows (not yet implemented)."""

    def is_app_running(self, process_names: list[str]) -> bool:
        raise NotImplementedError("Windows meeting detection is not yet implemented")

    def is_app_using_audio(self, process_names: list[str]) -> bool:
        raise NotImplementedError("Windows meeting detection is not yet implemented")

    def is_call_window_active(self) -> bool:
        raise NotImplementedError("Windows meeting detection is not yet implemented")
