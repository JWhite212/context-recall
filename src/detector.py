"""
Teams meeting detector for macOS.

Determines whether a Microsoft Teams meeting is currently active by
inspecting two independent signals:

1. Process-level: Is a Teams process running?
2. Audio-level:   Is Teams actively using an audio input device?

The combination of both signals gives high confidence that a live call
is in progress, avoiding false positives from simply having Teams open
in the background.

macOS-specific: uses `subprocess` calls to `pgrep` and `lsof` rather
than platform-agnostic libraries, since BlackHole and the rest of the
audio pipeline are inherently macOS-bound anyway.
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from enum import Enum, auto

from src.utils.config import DetectionConfig

logger = logging.getLogger(__name__)


class MeetingState(Enum):
    """Represents the current state of the meeting detector."""

    IDLE = auto()        # No meeting detected.
    ACTIVE = auto()      # Meeting in progress.
    ENDING = auto()      # Meeting just ended; transitioning to IDLE.


@dataclass
class MeetingEvent:
    """Emitted when a meeting starts or stops."""

    state: MeetingState
    started_at: float = 0.0    # Unix timestamp when the meeting began.
    ended_at: float = 0.0      # Unix timestamp when the meeting ended.
    duration_seconds: float = 0.0


class TeamsDetector:
    """
    Polls macOS process and audio state to detect Teams meetings.

    Usage:
        detector = TeamsDetector(config)
        detector.on_meeting_start = my_start_callback
        detector.on_meeting_end = my_end_callback
        detector.run()   # Blocking poll loop.
    """

    def __init__(self, config: DetectionConfig):
        self._config = config
        self._state = MeetingState.IDLE
        self._meeting_started_at: float = 0.0
        self._running = False
        self._consecutive_detections: int = 0

        # Callbacks — set these from the orchestrator.
        self.on_meeting_start: callable = lambda event: None
        self.on_meeting_end: callable = lambda event: None

    @property
    def state(self) -> MeetingState:
        return self._state

    # ------------------------------------------------------------------
    # Detection heuristics
    # ------------------------------------------------------------------

    def _is_teams_running(self) -> bool:
        """Check if any Teams process is currently running."""
        for name in self._config.process_names:
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

    def _is_teams_using_audio(self) -> bool:
        """
        Check if Teams has active audio device handles open.

        We look for specific file descriptors that indicate active audio
        streaming, not just loaded libraries. Teams always loads CoreAudio
        libraries but only opens device handles during a call.
        """
        for name in self._config.process_names:
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

                    # Only these patterns indicate active audio streaming
                    # (not just having libraries loaded):
                    active_indicators = [
                        "ioaudioengine",       # Kernel audio device handle.
                        "appleusbaudio",       # USB headset/mic device.
                        "blackhole",           # Virtual audio loopback.
                        "microsoftteamsaudio", # Teams' virtual audio device.
                    ]
                    if any(ind in output for ind in active_indicators):
                        return True

            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

        return False

    def _is_teams_call_window_active(self) -> bool:
        """
        Fallback heuristic: check if a Teams window title suggests an
        active call. Uses AppleScript to query window titles via
        System Events (requires Accessibility permissions).

        This is less reliable than the audio-device check but serves
        as a useful secondary signal.
        """
        script = '''
        tell application "System Events"
            set teamsList to every process whose name contains "Teams"
            repeat with teamsProc in teamsList
                set winNames to name of every window of teamsProc
                repeat with winName in winNames
                    set lower to do shell script "echo " & quoted form of (winName as text) & " | tr '[:upper:]' '[:lower:]'"
                    if lower contains "meeting" or lower contains "call with" or lower contains "in call" then
                        return true
                    end if
                end repeat
            end repeat
        end tell
        return false
        '''
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

    def _is_meeting_active(self) -> bool:
        """
        Combined detection: Teams must be running AND using audio.

        The window-title check is used as a tertiary signal only if
        the audio check is inconclusive. This avoids requiring
        Accessibility permissions for the common case.
        """
        if not self._is_teams_running():
            return False

        if self._is_teams_using_audio():
            return True

        # Fallback to window title inspection if audio check is negative.
        # This catches cases where Teams uses WebRTC audio that doesn't
        # appear in lsof as a CoreAudio handle.
        return self._is_teams_call_window_active()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Single poll cycle. Advances the state machine with debounce."""
        meeting_active = self._is_meeting_active()

        if self._state == MeetingState.IDLE:
            if meeting_active:
                self._consecutive_detections += 1
                required = self._config.required_consecutive_detections
                if self._consecutive_detections >= required:
                    self._meeting_started_at = time.time()
                    self._state = MeetingState.ACTIVE
                    self._consecutive_detections = 0
                    logger.info("Meeting confirmed — recording started.")
                    self.on_meeting_start(
                        MeetingEvent(
                            state=MeetingState.ACTIVE,
                            started_at=self._meeting_started_at,
                        )
                    )
                else:
                    logger.debug(
                        f"Possible meeting ({self._consecutive_detections}/"
                        f"{required} confirmations)"
                    )
            else:
                if self._consecutive_detections > 0:
                    logger.debug("Detection interrupted — resetting counter.")
                self._consecutive_detections = 0

        elif self._state == MeetingState.ACTIVE and not meeting_active:
            ended_at = time.time()
            duration = ended_at - self._meeting_started_at

            if duration < self._config.min_meeting_duration_seconds:
                logger.info(
                    f"Meeting lasted {duration:.0f}s (below "
                    f"{self._config.min_meeting_duration_seconds}s threshold) "
                    f"— discarding."
                )
            else:
                logger.info(
                    f"Meeting ended after {duration:.0f}s — processing."
                )
                self.on_meeting_end(
                    MeetingEvent(
                        state=MeetingState.ENDING,
                        started_at=self._meeting_started_at,
                        ended_at=ended_at,
                        duration_seconds=duration,
                    )
                )

            self._state = MeetingState.IDLE
            self._meeting_started_at = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Blocking poll loop. Call this from the main thread or a
        dedicated thread. Set self._running = False to stop.
        """
        self._running = True
        logger.info(
            f"Detector started. Polling every "
            f"{self._config.poll_interval_seconds}s."
        )
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"Detector tick error: {e}", exc_info=True)
            time.sleep(self._config.poll_interval_seconds)

    def stop(self) -> None:
        """Signal the poll loop to exit."""
        self._running = False
        logger.info("Detector stopped.")
