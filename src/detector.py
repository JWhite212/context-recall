"""
Teams meeting detector.

Determines whether a Microsoft Teams meeting is currently active by
inspecting two independent signals via a platform-specific detector:

1. Process-level: Is a Teams process running?
2. Audio-level:   Is Teams actively using an audio input device?

The combination of both signals gives high confidence that a live call
is in progress, avoiding false positives from simply having Teams open
in the background.

Platform-specific logic (pgrep, lsof, osascript on macOS) is isolated
in ``src/platform/`` implementations behind the ``PlatformDetector``
protocol, allowing future Windows/Linux support.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto

from src.platform.detector import PlatformDetector, create_detector
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

    def __init__(
        self,
        config: DetectionConfig,
        platform: PlatformDetector | None = None,
    ):
        self._config = config
        self._platform = platform or create_detector()
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
    # Detection heuristics (delegated to PlatformDetector)
    # ------------------------------------------------------------------

    def _is_meeting_active(self) -> bool:
        """
        Combined detection: Teams must be running AND using audio.

        The window-title check is used as a tertiary signal only if
        the audio check is inconclusive. This avoids requiring
        Accessibility permissions for the common case.
        """
        names = self._config.process_names

        if not self._platform.is_app_running(names):
            return False

        if self._platform.is_app_using_audio(names):
            return True

        # Fallback to window title inspection if audio check is negative.
        # This catches cases where Teams uses WebRTC audio that doesn't
        # appear in lsof as a CoreAudio handle.
        return self._platform.is_call_window_active()

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
