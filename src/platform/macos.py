"""
macOS meeting detection implementation.

Uses subprocess calls to pgrep, lsof, and osascript to detect active
meetings. These are inherently macOS-specific since BlackHole and the
rest of the audio pipeline are macOS-bound.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)


class MacOSDetector:
    """Detects meetings on macOS via process inspection and AppleScript."""

    def is_app_running(self, process_names: list[str]) -> bool:
        """Check if any of the given process names are currently running."""
        for name in process_names:
            try:
                result = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
        return False

    def is_app_using_audio(self, process_names: list[str]) -> bool:
        """
        Check if any of the given processes have active audio device handles.

        Looks for specific file descriptors that indicate active audio
        streaming, not just loaded libraries. Teams always loads CoreAudio
        libraries but only opens device handles during a call.
        """
        for name in process_names:
            try:
                pgrep = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if pgrep.returncode != 0:
                    continue

                pids = pgrep.stdout.strip().split("\n")
                for pid in pids:
                    pid = pid.strip()
                    if not pid:
                        continue

                    lsof = subprocess.run(
                        ["lsof", "-p", pid],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    output = lsof.stdout.lower()

                    active_indicators = [
                        "ioaudioengine",
                        "appleusbaudio",
                        "blackhole",
                        "microsoftteamsaudio",
                    ]
                    if any(ind in output for ind in active_indicators):
                        return True

            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

        return False

    def is_call_window_active(self) -> bool:
        """
        Fallback heuristic: check if a Teams window title suggests an
        active call via AppleScript (requires Accessibility permissions).
        """
        script = (  # noqa: E501
            'tell application "System Events"\n'
            '    set teamsList to every process whose name contains "Teams"\n'
            '    repeat with teamsProc in teamsList\n'
            '        set winNames to name of every window of teamsProc\n'
            '        repeat with winName in winNames\n'
            '            set lower to do shell script "echo "'
            ' & quoted form of (winName as text)'
            ' & " | tr \'[:upper:]\' \'[:lower:]\'"\n'
            '            if lower contains "meeting"'
            ' or lower contains "call with"'
            ' or lower contains "in call" then\n'
            "                return true\n"
            "            end if\n"
            "        end repeat\n"
            "    end repeat\n"
            "end tell\n"
            "return false"
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip().lower() == "true"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
