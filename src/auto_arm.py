"""Calendar-driven auto-arm controller.

Ticked from the detector poll loop. While a join-link calendar event is
within its armed window and nothing is recording, it opens an AudioMonitor
and watches for real meeting activity (system-audio RMS OR a meeting-app
process using audio). On activity it starts the orchestrator's recording;
it stops the recording it started once the clock passes end_ts + trailing.

All collaborators are injected so this is pure and unit-tested with fakes.
tick() never raises — the poll loop must survive any auto-arm failure.
"""

import logging
import time

logger = logging.getLogger(__name__)


class AutoArmController:
    def __init__(
        self,
        *,
        config,
        calendar_source,
        audio_monitor,
        process_active,
        is_recording,
        start,
        stop,
        clock=time.time,
    ) -> None:
        self._config = config
        self._calendar_source = calendar_source
        self._audio_monitor = audio_monitor
        self._process_active = process_active
        self._is_recording = is_recording
        self._start = start
        self._stop = stop
        self._clock = clock

        self._lead_seconds = config.lead_minutes * 60
        self._trailing_seconds = config.trailing_minutes * 60

        self._armed: bool = False  # monitor open
        self._recording_event: dict | None = None  # a recording we started

    def tick(self, now: float | None = None) -> None:
        """One poll cycle. Never raises (poll-loop resilience)."""
        try:
            self._tick(now if now is not None else self._clock())
        except Exception:
            logger.exception("Auto-arm tick failed — ignoring.")

    def _tick(self, now: float) -> None:
        recording = self._is_recording()

        # 1. Manage a recording we own.
        if self._recording_event is not None:
            if not recording:
                # Ended by other means (Teams-end / manual / silence watchdog).
                self._recording_event = None
            else:
                end_ts = self._recording_event.get("end_ts", 0.0)
                if now > end_ts + self._trailing_seconds:
                    self._recording_event = None
                    self._stop()
            return

        # 2. Someone else is recording — stay out of the way.
        if recording:
            self._disarm()
            return

        # 3. Idle: is a join-link event armed right now?
        event = self._calendar_source(now, self._lead_seconds)
        if event is None:
            self._disarm()
            return

        # 4. Armed — open the monitor and watch for activity.
        self._arm()
        if self._audio_monitor.active() or self._process_active():
            self._disarm()  # close before the real capture takes BlackHole
            self._recording_event = event
            self._start(event)

    def _arm(self) -> None:
        if not self._armed:
            self._audio_monitor.start()
            self._armed = True

    def _disarm(self) -> None:
        if self._armed:
            self._audio_monitor.stop()
            self._armed = False
