"""
Context Recall - main entry point and orchestrator.

Wires together the detector, audio capture, transcriber, summariser,
and output writers into a cohesive pipeline. Can run as:

  1. A daemon that auto-detects meetings:
       python -m src.main

  2. A one-shot recorder (skip detection, record immediately):
       python -m src.main --record-now

  3. Process an existing audio file (skip detection and capture):
       python -m src.main --process /path/to/audio.wav

The daemon mode is intended to be run via launchd on macOS so it
starts automatically on login and runs in the background.
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from src import calendar_permission
from src.audio_capture import AudioCapture, AudioCaptureError
from src.audio_cleanup import cleanup_temp_audio
from src.audio_monitor import AudioMonitor
from src.audio_preflight import run_preflight
from src.audio_routing import AudioRouter
from src.auto_arm import AutoArmController
from src.detector import MeetingEvent, MeetingState, TeamsDetector
from src.diariser import EnergyDiariser, create_diariser
from src.mic_permission import ensure_microphone_access
from src.output.markdown_writer import MarkdownWriter
from src.output.notion_writer import NotionWriter
from src.pipeline_runner import DbBridge, PipelineRunner
from src.silent_input_detector import SilentInputDetector
from src.summariser import Summariser
from src.system_audio import ScreenCaptureKitSystemCapture
from src.transcriber import Transcriber
from src.utils.config import load_config, materialise_default_config
from src.utils.paths import audio_dir as default_audio_dir

try:
    from src.calendar_matcher import CalendarMatch, CalendarMatcher
except ImportError:
    CalendarMatcher = None
    CalendarMatch = None

logger = logging.getLogger("contextrecall")


class _OrchestratorDbBridge(DbBridge):
    """DbBridge that routes meeting updates through ContextRecall._db_update.

    Keeps the orchestrator's C3 closed-loop logging (and the test seam
    that spies on ``_db_update``) authoritative for the live path while
    the shared PipelineRunner performs the actual pipeline work.
    """

    def __init__(self, app: "ContextRecall") -> None:
        server = app._api_server
        super().__init__(
            getattr(server, "repo", None) if server else None,
            getattr(server, "loop", None) if server else None,
            database=getattr(server, "db", None) if server else None,
        )
        self._app = app

    def update_meeting(self, meeting_id: str | None, **fields) -> None:
        self._app._db_update(meeting_id, **fields)


class ContextRecall:
    """
    Top-level orchestrator. Connects the detector's callbacks to
    the recording pipeline and manages the lifecycle of each
    meeting session.
    """

    def __init__(self, config_path: Path | None = None):
        # First boot on a fresh install: write the defaults so the
        # settings API has a real file to read and merge updates into.
        materialise_default_config(config_path)
        self._config = load_config(config_path)
        self._setup_logging()

        self._detector = TeamsDetector(self._config.detection)
        self._capture = AudioCapture(self._config.audio)
        self._audio_router = AudioRouter(blackhole_name=self._config.audio.blackhole_device_name)
        self._transcriber = Transcriber(self._config.transcription)
        self._summariser = Summariser(self._config.summarisation)
        self._diariser = (
            create_diariser(self._config.diarisation) if self._config.diarisation.enabled else None
        )

        # Energy backend needs separate source files; pyannote uses combined audio.
        if self._diariser and isinstance(self._diariser, EnergyDiariser):
            self._config.audio.keep_source_files = True

        # Output writers (initialised based on config).
        self._md_writer = (
            MarkdownWriter(self._config.markdown) if self._config.markdown.enabled else None
        )
        self._notion_writer = (
            NotionWriter(self._config.notion) if self._config.notion.enabled else None
        )

        self._meeting_started_at: float = 0.0
        self._active_meeting_id: str | None = None

        # Remembers the last capture warning already surfaced as a
        # pipeline.warning, so the post-merge SCK "grant Screen Recording"
        # warning (set at the end of the capture loop, after the start-time
        # _emit_capture_warnings() window) is emitted exactly once and a
        # start-time mic warning is never re-emitted (I2).
        self._last_emitted_capture_warning: str | None = None

        # Background processing executor for non-blocking pipeline runs.
        self._processing_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pipeline"
        )
        self._processing_futures: list[concurrent.futures.Future] = []

        # API server and event system (initialised lazily in run_daemon).
        self._api_server = None
        self._event_bus = None

        # Calendar auto-arm controller (constructed at daemon boot when enabled).
        self._auto_arm = None

        # Shutdown coordination for run_daemon. The signal handler sets the
        # event; a watcher thread translates that into detector.stop() and
        # capture teardown, so signal-handler code never re-enters into
        # pipeline threads.
        self._shutdown_event = threading.Event()
        self._signal_handler_invocations = 0
        self._previous_signal_handlers: dict[int, object] = {}

        # Live transcription (optional).
        self._live_transcriber = None

        # Detects "BlackHole installed but not routed" — the audio stream
        # opens fine but delivers only silence. Surfaces a one-shot warning
        # so the user can fix routing while the meeting is still in flight,
        # rather than discovering it from an empty transcript at the end.
        self._silent_input_detector = SilentInputDetector(
            silence_threshold=self._config.audio.silence_alert_threshold,
        )

        # Calendar integration (optional).
        self._calendar_matcher: CalendarMatcher | None = None
        self._calendar_match: CalendarMatch | None = None
        if CalendarMatcher and self._config.calendar.enabled:
            self._calendar_matcher = CalendarMatcher(
                time_window_minutes=self._config.calendar.time_window_minutes,
                min_confidence=self._config.calendar.min_confidence,
            )
            if self._calendar_matcher.available:
                logger.info("Calendar integration enabled")
            else:
                # Do NOT null the matcher: it is typically just not
                # authorized YET (the boot poller raises the prompt after
                # construction) and self-heals via match() once granted.
                logger.warning(
                    "Calendar integration enabled but not authorized yet — "
                    "matching activates once calendar access is granted"
                )

        # Wire up detector callbacks.
        self._detector.on_meeting_start = self._on_meeting_start
        self._detector.on_meeting_end = self._on_meeting_end

    def _setup_logging(self) -> None:
        """Configure logging to both console and file.

        Uses RotatingFileHandler so a long-running daemon doesn't grow an
        unbounded log file: at 10 MiB the active log rolls over and up to
        5 historical copies (.1 ... .5) are kept before older ones are
        discarded.
        """
        log_level = getattr(logging, self._config.logging.level.upper(), logging.INFO)
        log_file = self._config.logging.log_file

        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.handlers.RotatingFileHandler(
                    log_file,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                ),
            ],
        )

        # Third-party HTTP clients log one INFO line per request; the
        # stdout copy lands in a launchd-captured file nothing rotates
        # (11 MB observed in half a day during a model download).
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("huggingface_hub").setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, **kwargs) -> None:
        """Emit an event if the EventBus is active."""
        if self._event_bus:
            self._event_bus.emit({"type": event_type, **kwargs})

    def _get_daemon_state(self) -> str:
        """Return current daemon state for the API."""
        if self._capture.is_recording:
            return "recording"
        state = self._detector.state
        if state == MeetingState.ACTIVE:
            return "recording"
        return "idle"

    def _get_active_meeting(self) -> dict | None:
        """Return info about the active meeting, or None."""
        if not self._capture.is_recording:
            return None
        return {
            "meeting_id": self._active_meeting_id,
            "started_at": self._meeting_started_at,
            "elapsed_seconds": time.time() - self._meeting_started_at,
        }

    # ------------------------------------------------------------------
    # Detector callbacks
    # ------------------------------------------------------------------

    def _emit_capture_warnings(self) -> None:
        """Surface any non-fatal warning AudioCapture recorded during start.

        Called after _capture.start() returns successfully so the user sees
        actionable hints (e.g. "configured mic not found, recording system
        audio only") via the same pipeline.warning banner the silent-input
        detector uses (Bug A4).
        """
        warning = getattr(self._capture, "last_warning", None)
        if warning:
            self._last_emitted_capture_warning = str(warning)
            self._emit("pipeline.warning", source="mic", message=str(warning))

    def _emit_late_capture_warning(self) -> None:
        """Surface a capture warning that appeared AFTER start() (I2).

        The SCK backend records its "grant Screen Recording" hint at the end
        of the capture loop (ScreenCaptureKitSystemCapture.stop), which is
        after _emit_capture_warnings() already ran. Called once the merge is
        done; deduplicated against whatever _emit_capture_warnings already
        emitted so a start-time mic warning is never repeated.
        """
        warning = getattr(self._capture, "last_warning", None)
        if warning and str(warning) != self._last_emitted_capture_warning:
            self._last_emitted_capture_warning = str(warning)
            self._emit("pipeline.warning", source="system", message=str(warning))

    def _ensure_audio_routing(self) -> None:
        """Route system audio into the capture loopback before recording.

        Failures are warnings, not errors: mic capture still works
        without routing, and the silent-input detector will flag the
        missing system audio while the meeting is in flight.
        """
        if not self._config.audio.auto_route_system_audio:
            return
        result = self._audio_router.ensure_routed()
        if result.error:
            self._emit("pipeline.warning", source="routing", message=result.error)
        elif result.changed:
            self._emit("audio.routing", message=result.message)

    def _restore_audio_routing(self) -> None:
        """Switch the default output back after recording (best-effort)."""
        if not self._config.audio.auto_route_system_audio:
            return
        try:
            self._audio_router.restore()
        except Exception:
            logger.exception("Restoring audio routing failed")

    def _wire_audio_level_callback(self) -> None:
        """Install the audio.level callback used by both auto-detect and
        manual-recording entry points. Resets the silent-input detector
        for the new session and emits pipeline.warning when prolonged
        silence is observed on the system source."""
        self._silent_input_detector.reset()
        # Scope the late-capture-warning dedup to this session so an identical
        # SCK "grant Screen Recording" warning re-surfaces on a later meeting.
        self._last_emitted_capture_warning = None

        def _on_level(system_rms: float, mic_rms: float) -> None:
            self._emit(
                "audio.level",
                system_rms=round(system_rms, 6),
                mic_rms=round(mic_rms, 6),
            )
            if self._silent_input_detector.observe(system_rms=system_rms, now=time.monotonic()):
                backend = getattr(self._capture, "_system_backend", None)
                if isinstance(backend, ScreenCaptureKitSystemCapture):
                    logger.warning(
                        "System audio source delivered silence for the alert "
                        "window — Screen Recording may not be granted."
                    )
                    message = (
                        "No system audio detected. Grant Screen Recording in "
                        "System Settings → Privacy & Security → Screen Recording, "
                        "then re-record."
                    )
                else:
                    logger.warning(
                        "System audio source delivered silence for the alert "
                        "window — BlackHole may be installed but not routed."
                    )
                    message = (
                        "No system audio detected. If you are using BlackHole, "
                        "make sure your system output is routed to it via a "
                        "Multi-Output Device in Audio MIDI Setup."
                    )
                self._emit(
                    "pipeline.warning",
                    type="silent_input",
                    source="system",
                    message=message,
                )

        self._capture.on_audio_level = _on_level

    def _wire_capture_error_callbacks(self) -> None:
        """Install on_capture_error and on_stream_status so the orchestrator
        surfaces capture-thread failures and stream-status flags as
        pipeline.error / pipeline.warning events the moment they happen,
        rather than discovering them after wait_for_merge times out."""

        def _on_capture_error(err: AudioCaptureError) -> None:
            self._emit("pipeline.error", stage="capture", error=str(err))
            # The capture thread is dead, so no stop() will ever run (a
            # manual stop now 409s on "Not recording") — hand the output
            # device back immediately or the managed multi-output stays
            # the user's default with the volume keys disabled.
            self._restore_audio_routing()

        self._capture.on_capture_error = _on_capture_error
        self._capture.on_stream_status = lambda source, status: self._emit(
            "pipeline.warning",
            source=source,
            message=f"Audio stream status: {status}",
        )

    def _ensure_mic_permission(self) -> str | None:
        """Gate every recording start on the macOS microphone TCC grant.

        Without it CoreAudio silently records zeros from every input
        (including the BlackHole loopback) or fails stream.start() with
        PortAudio -9986 — both observed in production on 2026-07-07
        after the app rename invalidated the old path-bound grant.
        Fires the system prompt when the status is still undetermined.

        Returns None when recording may proceed, else the user-facing
        problem (already emitted as pipeline.error).
        """
        status, problem = ensure_microphone_access()
        if problem is None:
            return None
        logger.error("Microphone permission gate blocked recording (%s): %s", status, problem)
        self._emit("pipeline.error", stage="permission", error=problem)
        return problem

    def _request_mic_permission_at_boot(self) -> None:
        """Raise the TCC prompt at daemon start when still undetermined.

        Uses the implicit path (briefly opening an input stream): tccd
        KILLS a launchd daemon for the explicit AVCaptureDevice request
        even with the usage description sealed into the bundle (observed
        2026-07-07, OS_REASON_TCC crash loop), while the implicit request
        merely prompts. Polls for the user's answer so the log records
        the outcome.
        """
        from src.mic_permission import (
            NOT_DETERMINED,
            authorization_status,
            trigger_prompt_via_input_probe,
        )

        status = authorization_status()
        if status != NOT_DETERMINED:
            logger.info("Microphone permission at boot: %s", status)
            return
        logger.info(
            "Microphone permission undetermined — raising the system dialog via an input probe."
        )
        trigger_prompt_via_input_probe()
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            status = authorization_status()
            if status != NOT_DETERMINED:
                break
            time.sleep(2.0)
        logger.info("Microphone permission at boot: %s", status)

    def _request_calendar_permission_at_boot(self) -> None:
        """Raise the calendar TCC prompt at daemon start when undetermined.

        Runs on its own daemon thread — the dialog can sit unanswered for
        minutes. Delegates the request+poll to calendar_permission so the
        logic stays unit-tested there."""
        status = calendar_permission.request_access_at_boot()
        logger.info("Calendar permission at boot: %s", status)

    def _request_screen_recording_at_boot(self) -> None:
        """Register the daemon in the Screen Recording list at boot when SCK
        is the active system-audio backend.

        ScreenCaptureKit captures system audio via the Screen Recording TCC
        service. A launchd daemon can't be added through System Settings' "+"
        (that resolves the nested daemon app to the outer bundle), and a failed
        capture never registers it, so we call CGRequestScreenCaptureAccess()
        to register the daemon's own code identity — the user then toggles it
        on to capture system audio. Own thread: the request can surface a
        prompt that sits unanswered. No-op when BlackHole is the backend."""
        try:
            from src.system_audio import (
                ScreenCaptureKitSystemCapture,
                select_system_backend,
            )

            backend = select_system_backend(self._config.audio)
        except Exception:
            logger.debug("Screen Recording boot check skipped", exc_info=True)
            return
        if not isinstance(backend, ScreenCaptureKitSystemCapture):
            return  # BlackHole backend needs the mic grant, not Screen Recording.

        from src.screen_recording_permission import (
            GRANTED,
            request_screen_recording_access,
            screen_recording_status,
        )

        if screen_recording_status() == GRANTED:
            logger.info("Screen Recording permission at boot: granted.")
            return
        logger.info(
            "Screen Recording not yet granted — registering the daemon via "
            "CGRequestScreenCaptureAccess so it appears in System Settings → "
            "Privacy & Security → Screen Recording. Enable it there to capture "
            "system audio via ScreenCaptureKit."
        )
        request_screen_recording_access()
        logger.info("Screen Recording permission at boot: %s", screen_recording_status())

    def _on_meeting_start(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting begins."""
        logger.info("Starting audio capture...")

        if self._ensure_mic_permission() is not None:
            return

        # Pre-flight audio environment check. Surfaces missing BlackHole,
        # mic permission denial, etc. before we open any streams so the
        # user gets an actionable error instead of an empty recording.
        try:
            # refresh=True unless a stream is somehow already open —
            # re-initialising PortAudio invalidates open streams.
            preflight = run_preflight(self._config.audio, refresh=not self._capture.is_recording)
        except Exception as e:
            logger.warning("Audio pre-flight check failed: %s", e, exc_info=True)
        else:
            for warning in preflight.warnings:
                self._emit("pipeline.warning", source="preflight", message=warning)
            for err in preflight.errors:
                self._emit("pipeline.error", stage="preflight", error=err)
            if preflight.errors:
                logger.error(
                    "Aborting meeting start — pre-flight reported %d error(s).",
                    len(preflight.errors),
                )
                return

        self._ensure_audio_routing()
        self._wire_audio_level_callback()
        self._wire_capture_error_callbacks()

        try:
            self._capture.start()
        except Exception as e:
            logger.error("Failed to start audio capture: %s", e, exc_info=True)
            self._emit("pipeline.error", stage="capture", error=str(e))
            self._restore_audio_routing()
            return

        self._emit_capture_warnings()

        # Only update state after capture has started successfully.
        self._meeting_started_at = event.started_at or time.time()
        self._emit("meeting.started", started_at=self._meeting_started_at)

        # Match meeting to calendar event.
        self._calendar_match = None
        if self._calendar_matcher:
            try:
                self._calendar_match = self._calendar_matcher.match(self._meeting_started_at)
                if self._calendar_match:
                    logger.info(
                        "Calendar match: %s (%.0f%% confidence)",
                        self._calendar_match.event_title,
                        self._calendar_match.confidence * 100,
                    )
                    self._emit(
                        "meeting.calendar_match",
                        title=self._calendar_match.event_title,
                        attendees=[a["name"] for a in self._calendar_match.attendees],
                        confidence=self._calendar_match.confidence,
                    )
            except Exception as e:
                logger.warning("Calendar matching failed: %s", e)

        # Start live transcription if enabled.
        if self._config.transcription.live_enabled:
            try:
                from src.live_transcriber import LiveTranscriber, LiveTranscriptionConfig

                live_config = LiveTranscriptionConfig(
                    chunk_interval_seconds=self._config.transcription.live_chunk_interval,
                )

                def _on_live_segment(seg):
                    self._emit(
                        "transcript.segment",
                        meeting_id=self._active_meeting_id,
                        segment=asdict(seg),
                    )

                def _on_live_warning(payload: dict) -> None:
                    # Forward structured live-transcriber warnings (e.g.
                    # type=live_chunk_drop) onto the pipeline.warning bus
                    # so the UI can surface backpressure to the user.
                    self._emit("pipeline.warning", **payload)

                self._live_transcriber = LiveTranscriber(
                    model_size=self._config.transcription.model_size,
                    language=self._config.transcription.language,
                    on_segment=_on_live_segment,
                    sample_rate=self._config.audio.sample_rate,
                    config=live_config,
                    on_warning=_on_live_warning,
                )
                self._capture.on_audio_data = self._live_transcriber.feed
                self._live_transcriber.start()
            except Exception as e:
                logger.warning("Failed to start live transcription: %s", e)
                self._live_transcriber = None

    def _on_meeting_end(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting ends."""
        logger.info("Stopping audio capture and processing...")

        # Stop live transcriber before batch processing to free GPU.
        # live_transcriber.stop() joins its worker thread with a 30s timeout;
        # running that synchronously here would block the detector callback
        # thread and cause back-to-back meetings to be missed (X4). Dispatch
        # to a daemon thread and clear references synchronously so a fresh
        # meeting can't observe stale state.
        if self._live_transcriber:
            lt = self._live_transcriber
            self._live_transcriber = None
            self._capture.on_audio_data = None

            def _stop_live_transcriber() -> None:
                try:
                    lt.stop()
                except Exception:
                    logger.exception("Background live_transcriber.stop() failed")

            threading.Thread(
                target=_stop_live_transcriber,
                name="live-transcriber-stop",
                daemon=True,
            ).start()

        self._emit(
            "meeting.ended",
            duration=event.duration_seconds,
        )
        try:
            audio_path = self._capture.stop(blocking=False)
        except TypeError:
            audio_path = self._capture.stop()

        # Hand the output device back to the user regardless of whether
        # capture produced a usable file.
        self._restore_audio_routing()

        if audio_path is None or not audio_path.exists():
            logger.error("No audio file produced. Skipping processing.")
            return

        # Capture meeting_id before clearing so background thread has it.
        meeting_id = self._active_meeting_id

        # Clear active meeting ID so the next meeting can start fresh.
        self._active_meeting_id = None

        # Remove completed futures.
        self._processing_futures = [f for f in self._processing_futures if not f.done()]

        # Cap concurrent in-flight pipelines. Past this point the executor
        # would queue work indefinitely if the pipeline thread stalls (e.g.
        # MLX hung, Ollama unreachable), so the daemon would silently grow
        # an unbounded backlog of meeting jobs and the future list. Surface
        # the saturation loudly so it shows up in the UI instead of as a
        # memory leak.
        if len(self._processing_futures) >= 16:
            err = RuntimeError("too many concurrent pipelines")
            self._emit(
                "pipeline.error",
                meeting_id=meeting_id,
                stage="dispatch",
                error=str(err),
            )
            raise err

        # Snapshot live-session context NOW: a back-to-back meeting can
        # overwrite self._calendar_match and the capture's source paths
        # before the executor thread gets around to reading them.
        calendar_match = self._calendar_match
        mic_path = self._capture.mic_audio_path if self._config.audio.keep_source_files else None

        future = self._processing_executor.submit(
            self._process_audio,
            audio_path=audio_path,
            started_at=event.started_at,
            duration_seconds=event.duration_seconds,
            meeting_id=meeting_id,
            calendar_match=calendar_match,
            mic_audio_path=mic_path,
        )
        self._processing_futures.append(future)

        # Log any exceptions from the background thread.
        future.add_done_callback(self._on_processing_done)

    def _on_processing_done(self, future: concurrent.futures.Future) -> None:
        """Log exceptions from background processing threads."""
        try:
            future.result()
        except Exception:
            logger.error("Background processing failed", exc_info=True)
        self._sweep_temp_audio()

    def _sweep_temp_audio(self) -> None:
        """Remove empty stubs and stale recordings from the temp dir.

        Runs at daemon start and after every pipeline completes, sparing
        whatever the capture session currently has open.
        """
        try:
            active = self._capture.active_temp_paths if self._capture.is_recording else ()
            cleanup_temp_audio(
                self._config.audio.temp_audio_dir,
                max_age_days=self._config.audio.temp_retention_days,
                active_paths=active,
            )
        except Exception:
            logger.exception("Temp-audio sweep failed")

    # ------------------------------------------------------------------
    # Audio persistence
    # ------------------------------------------------------------------

    def _persist_audio(
        self,
        audio_path: Path,
        started_at: float,
        *,
        meeting_id: str | None = None,
        status: str = "transcribing",
    ) -> tuple[Path, str | None]:
        """Persist audio to a durable location and create a DB record.

        Returns (persistent_audio_path, meeting_id).
        """
        persistent_audio_path = audio_path
        if self._api_server and self._api_server.repo:
            audio_dir = default_audio_dir()
            audio_dir.mkdir(parents=True, exist_ok=True)
            persistent_audio_path = (audio_dir / audio_path.name).resolve()
            if not str(persistent_audio_path).startswith(str(audio_dir.resolve())):
                raise ValueError(
                    f"Refusing to write audio outside audio_dir: {persistent_audio_path}"
                )
            if audio_path != persistent_audio_path:
                try:
                    os.link(audio_path, persistent_audio_path)
                except OSError:
                    shutil.copy2(audio_path, persistent_audio_path)

        if self._api_server and self._api_server.repo and self._api_server.loop:
            loop = self._api_server.loop
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._api_server.repo.create_meeting(started_at=started_at, status=status),
                    loop,
                )
                meeting_id = future.result(timeout=5)
                self._active_meeting_id = meeting_id
            except Exception as e:
                # %r: a bare TimeoutError stringifies to '' — the live log
                # once showed "Failed to create meeting record: " with no
                # clue what happened.
                logger.warning("Failed to create meeting record: %r", e)
            else:
                try:
                    update_future = asyncio.run_coroutine_threadsafe(
                        self._api_server.repo.update_meeting(
                            meeting_id,
                            audio_path=str(persistent_audio_path),
                        ),
                        loop,
                    )
                    # A row without audio_path can never be processed (the
                    # UI hides Process/Retry and /reprocess returns 400),
                    # so this write must not be fire-and-forget.
                    update_future.result(timeout=5)
                except Exception as e:
                    logger.error(
                        "Meeting %s was created without audio_path (%r); "
                        "startup relink will repair it if %s survives",
                        meeting_id,
                        e,
                        persistent_audio_path.name,
                    )

        return persistent_audio_path, meeting_id

    # ------------------------------------------------------------------
    # Processing pipeline
    # ------------------------------------------------------------------

    def _process_audio(
        self,
        audio_path: Path,
        started_at: float = 0.0,
        duration_seconds: float = 0.0,
        meeting_id: str | None = None,
        calendar_match=None,
        mic_audio_path: Path | None = None,
    ) -> None:
        """
        Run the full pipeline on a captured audio file:
        transcribe -> summarise -> write outputs.

        If the API server is running, persists the meeting to the
        database and emits events for real-time UI updates.
        """
        if started_at == 0.0:
            started_at = time.time()

        if meeting_id is None:
            meeting_id = self._active_meeting_id

        persistent_audio_path, meeting_id = self._persist_audio(
            audio_path, started_at, meeting_id=meeting_id, status="transcribing"
        )

        # Wait for the audio merge if stop was non-blocking. Only when a
        # capture session actually ran: in --process mode (and reprocess)
        # there is no session, so the merge event would never fire and
        # the old unconditional wait stalled 120s then skipped the file.
        if getattr(self._capture, "merge_pending", False):
            if not self._capture.wait_for_merge(timeout=120):
                logger.error("Audio merge timed out after 120s. Skipping processing.")
                self._emit(
                    "pipeline.error",
                    meeting_id=meeting_id,
                    stage="capture",
                    error="Audio merge timed out — capture may have hung.",
                )
                self._db_update(meeting_id, status="error")
                return
            # The capture loop sets its backend warning (e.g. the SCK "grant
            # Screen Recording" hint) only at the very end, after the merge —
            # too late for the start-time _emit_capture_warnings(). Surface it
            # now, idempotently so a start-time mic warning isn't repeated (I2).
            self._emit_late_capture_warning()

        # If the capture thread reported a typed error (e.g. mic permission
        # denied, device unavailable), surface it before pretending we have
        # an audio file to transcribe.
        capture_error = getattr(self._capture, "last_error", None)
        if isinstance(capture_error, AudioCaptureError):
            logger.error("Audio capture failed: %s", capture_error)
            self._emit(
                "pipeline.error",
                meeting_id=meeting_id,
                stage="capture",
                error=str(capture_error),
            )
            self._db_update(meeting_id, status="error")
            return

        # Everything from transcription onward is shared with the
        # reprocess route via PipelineRunner. The orchestrator only
        # contributes its live-session context: the calendar match, the
        # surviving mic source WAV, and a DB bridge that routes meeting
        # updates through _db_update (C3 logging + test seam).
        # Callers that dispatch to the executor snapshot these; direct
        # single-flight callers (--record-now, --process, shutdown drain)
        # fall back to the live attributes.
        if calendar_match is None:
            calendar_match = self._calendar_match
        if mic_audio_path is None:
            mic_audio_path = (
                self._capture.mic_audio_path if self._config.audio.keep_source_files else None
            )

        attendees: list[dict] = []
        calendar_fields = None
        if calendar_match:
            attendees = calendar_match.attendees or []
            calendar_fields = {
                "calendar_event_title": calendar_match.event_title,
                "attendees_json": json.dumps(calendar_match.attendees),
                "calendar_confidence": calendar_match.confidence,
                "teams_join_url": calendar_match.teams_join_url,
                "teams_meeting_id": calendar_match.teams_meeting_id,
                "calendar_event_uid": calendar_match.event_uid,
            }

        runner = PipelineRunner(
            self._config,
            emit=self._emit,
            db=_OrchestratorDbBridge(self),
            transcriber=self._transcriber,
            summariser=self._summariser,
            diariser=self._diariser,
            md_writer=self._md_writer,
            notion_writer=self._notion_writer,
        )
        runner.run(
            persistent_audio_path,
            meeting_id,
            started_at,
            duration_seconds,
            attendees=attendees,
            mic_audio_path=mic_audio_path,
            calendar_fields=calendar_fields,
        )

        self._active_meeting_id = None
        logger.info("Processing complete.")

    def _db_update(self, meeting_id: str | None, **fields) -> None:
        """Update a meeting record in the database (fire-and-forget).

        Logs failures but does not raise, so the pipeline continues
        even if the DB write fails.
        """
        if not meeting_id or not self._api_server or not self._api_server.repo:
            return
        loop = self._api_server.loop
        if not loop or loop.is_closed():
            # The API server existed when this update was scheduled but is
            # now torn down (Bug C3). The pipeline thread will continue
            # silently, leaving the row in whatever transient status the
            # last successful update wrote. Surface this loudly so on-call
            # can correlate stuck rows with the daemon shutdown.
            logger.error(
                "DB update for meeting %s dropped: event loop is %s. Fields lost: %s",
                meeting_id,
                "closed" if loop and loop.is_closed() else "missing",
                sorted(fields.keys()),
            )
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._api_server.repo.update_meeting(meeting_id, **fields),
                loop,
            )

            def _log_db_error(fut):
                exc = fut.exception()
                if exc:
                    logger.error("DB update failed for meeting %s: %s", meeting_id, exc)

            future.add_done_callback(_log_db_error)
        except Exception:
            logger.error(
                "Failed to schedule DB update for meeting %s",
                meeting_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Manual recording (called from API)
    # ------------------------------------------------------------------

    def api_start_recording(self) -> None:
        """Start a manual recording session via the API.

        Raises AudioCaptureError if a recording is already in progress, the
        microphone permission is missing, or the audio device cannot be
        opened.
        """
        # Guard here, not just in AudioCapture: its start() would no-op on a
        # live capture, after which this method would reset
        # _meeting_started_at and emit a spurious meeting.started for the
        # recording already in flight (manual + auto-arm can race).
        if self._capture.is_recording:
            raise AudioCaptureError("A recording is already in progress")

        problem = self._ensure_mic_permission()
        if problem is not None:
            raise AudioCaptureError(problem)

        self._ensure_audio_routing()
        self._wire_audio_level_callback()
        self._wire_capture_error_callbacks()

        try:
            self._capture.start()
        except Exception:
            logger.error("API recording start failed", exc_info=True)
            self._emit("pipeline.error", stage="capture", error="Failed to start audio capture")
            self._restore_audio_routing()
            raise

        self._emit_capture_warnings()
        self._meeting_started_at = time.time()
        self._emit("meeting.started", started_at=self._meeting_started_at)

    def api_stop_recording(self) -> None:
        """Stop a manual recording and trigger background processing."""
        started_at = self._meeting_started_at
        self._emit("meeting.ended", duration=time.time() - started_at)
        audio_path = self._capture.stop()
        self._restore_audio_routing()

        if audio_path and audio_path.exists():
            duration = time.time() - started_at
            self._processing_executor.submit(
                self._process_audio,
                audio_path,
                started_at,
                duration,
            )

    def api_stop_recording_deferred(self) -> str:
        """Stop a manual recording but defer processing.

        Persists the audio file and creates a meeting record with
        status ``"pending"`` so the user can trigger processing later.

        Returns the meeting ID.
        """
        started_at = self._meeting_started_at
        duration = time.time() - started_at
        self._emit("meeting.ended", duration=duration)
        audio_path = self._capture.stop()
        self._restore_audio_routing()

        if not audio_path or not audio_path.exists():
            raise AudioCaptureError("No audio file produced")

        persistent_path, meeting_id = self._persist_audio(audio_path, started_at, status="pending")
        if self._api_server is not None and not meeting_id:
            # Without a row the recording is invisible in the UI. Raise so
            # the API returns 500 instead of toasting "Recording saved."
            raise AudioCaptureError(
                f"Recording audio was saved to {persistent_path.name}, but the "
                "meeting record could not be created. Check daemon logs."
            )
        if meeting_id:
            self._db_update(meeting_id, duration_seconds=duration, ended_at=started_at + duration)
        return meeting_id or ""

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def _start_api_server(self) -> None:
        """Start the API server on a background thread if enabled."""
        if not self._config.api.enabled:
            return

        from src.api.events import EventBus
        from src.api.server import ApiServer

        self._event_bus = EventBus()
        self._api_server = ApiServer(
            host=self._config.api.host,
            port=self._config.api.port,
            event_bus=self._event_bus,
        )
        self._api_server.set_state_accessors(
            self._get_daemon_state,
            self._get_active_meeting,
        )
        self._api_server.set_recording_controls(
            start=self.api_start_recording,
            stop=self.api_stop_recording,
            stop_deferred=self.api_stop_recording_deferred,
            is_recording=lambda: self._capture.is_recording,
        )
        self._api_server.start()

        # Give the server a moment to bind.
        time.sleep(0.5)

    def _calendar_source(self, now: float, lead_seconds: float) -> dict | None:
        """Resolve the currently-armed join-link event via the API loop.

        Bounded and best-effort: the controller only calls this while NOT
        recording (the API loop is idle then), so the short blocking wait
        can't stack behind a running pipeline.
        """
        import asyncio

        from src.calendar_events.repository import CalendarEventRepository

        server = self._api_server
        if server is None:
            return None
        loop = server.loop
        if loop is None or loop.is_closed():
            return None
        try:
            repo = CalendarEventRepository(server.db)
            future = asyncio.run_coroutine_threadsafe(
                repo.current_join_link_event(now, lead_seconds), loop
            )
            return future.result(timeout=2.0)
        except Exception:
            logger.debug("Auto-arm calendar lookup failed", exc_info=True)
            return None

    def _auto_arm_process_active(self) -> bool:
        """Meeting-app process signal for auto-arm (best-effort)."""
        try:
            return self._detector.app_using_audio(self._config.auto_arm.meeting_process_names)
        except Exception:
            logger.debug("Auto-arm process check failed", exc_info=True)
            return False

    def _auto_arm_start(self, event: dict) -> None:
        """Start recording for an armed event (mic-gate failures are no-ops)."""
        try:
            logger.info("Auto-arm: activity detected — starting recording.")
            self.api_start_recording()
        except AudioCaptureError as exc:
            logger.warning("Auto-arm start blocked: %s", exc)

    def _auto_arm_stop(self) -> None:
        """Stop an auto-armed recording without blocking the poll thread.

        api_stop_recording() blocks up to 30s on the post-merge; run it on a
        throwaway daemon thread so detection keeps polling.
        """
        threading.Thread(
            target=self.api_stop_recording,
            name="auto-arm-stop",
            daemon=True,
        ).start()

    def _maybe_start_auto_arm(self) -> None:
        """Construct + wire the auto-arm controller when opted in."""
        if not (self._config.auto_arm.enabled and self._config.calendar.import_enabled):
            return
        monitor = AudioMonitor(
            blackhole_device_name=self._config.audio.blackhole_device_name,
            sample_rate=self._config.audio.sample_rate,
            threshold_dbfs=self._config.auto_arm.activity_rms_dbfs,
            sustain_seconds=self._config.auto_arm.activity_sustain_seconds,
        )
        self._auto_arm = AutoArmController(
            config=self._config.auto_arm,
            calendar_source=self._calendar_source,
            audio_monitor=monitor,
            process_active=self._auto_arm_process_active,
            is_recording=lambda: self._capture.is_recording,
            start=self._auto_arm_start,
            stop=self._auto_arm_stop,
            clock=time.time,
        )
        self._detector.on_tick = self._auto_arm.tick
        logger.info("Calendar auto-arm enabled.")

    def _shutdown_watcher(self) -> None:
        """Translate the shutdown event into detector + capture teardown.

        Runs on a dedicated daemon thread so the signal handler never
        re-enters detector or capture code from arbitrary threads while
        the pipeline might be holding their internal locks.
        """
        self._shutdown_event.wait()
        logger.info("Shutdown watcher: tearing down detector and capture.")
        try:
            self._detector.stop()
        except Exception:
            logger.exception("Shutdown watcher: detector.stop() failed")
        try:
            if self._capture.is_recording:
                self._capture.stop(blocking=False)
        except Exception:
            logger.exception("Shutdown watcher: capture.stop() failed")
        # Never leave the user's output pointed at the managed device
        # across a daemon shutdown.
        self._restore_audio_routing()

    def _install_signal_handlers(self) -> None:
        """Install idempotent SIGINT/SIGTERM handlers.

        First delivery sets `_shutdown_event` so the watcher thread can run
        graceful teardown on the main path. Second delivery restores the
        original handler so the next signal terminates the process via the
        platform default — preventing a hung shutdown from being un-killable.
        """

        def _handler(signum, frame):
            self._signal_handler_invocations += 1
            logger.info(
                "Shutdown signal %s received (delivery #%d).",
                signum,
                self._signal_handler_invocations,
            )
            if self._signal_handler_invocations == 1:
                self._shutdown_event.set()
                return
            # Second delivery: restore default behaviour and re-raise so the
            # process exits even if graceful shutdown is wedged.
            previous = self._previous_signal_handlers.get(signum, signal.SIG_DFL)
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_signal_handlers[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError) as e:
                # signal.signal may fail when not on the main thread (e.g.
                # in some test contexts). Log and continue — the watcher
                # thread can still be driven from inside the process.
                logger.warning("Could not install handler for signal %s: %s", sig, e)

    def run_daemon(self) -> None:
        """
        Run as a background daemon, polling for Teams meetings.
        Blocks until interrupted with SIGINT/SIGTERM.
        """
        logger.info("Context Recall daemon starting...")

        # Start the API server for UI communication.
        self._start_api_server()

        # Wire calendar auto-arm (opt-in) now that the API loop exists.
        self._maybe_start_auto_arm()

        # Trigger the microphone permission dialog at boot (rather than
        # mid-meeting) when macOS has not asked yet. Own thread: the
        # dialog can sit unanswered for minutes.
        threading.Thread(
            target=self._request_mic_permission_at_boot,
            name="mic-permission",
            daemon=True,
        ).start()

        # Trigger the calendar permission dialog at boot too, on its own
        # thread for the same reason (the dialog can sit unanswered).
        threading.Thread(
            target=self._request_calendar_permission_at_boot,
            name="calendar-permission",
            daemon=True,
        ).start()

        # Register the daemon in the Screen Recording list at boot when SCK is
        # the system-audio backend — it can't be added via System Settings' "+"
        # (that resolves to the outer bundle), so the daemon must request under
        # its own identity for the user to be able to grant it. Own thread.
        threading.Thread(
            target=self._request_screen_recording_at_boot,
            name="screen-recording-permission",
            daemon=True,
        ).start()

        # Sweep recording debris left in the temp dir (Caches) by failed
        # or long-gone sessions.
        threading.Thread(
            target=self._sweep_temp_audio,
            name="temp-audio-cleanup",
            daemon=True,
        ).start()

        # Wire shutdown plumbing: a tiny daemon thread waits on the event
        # flag and drives the actual teardown, so the signal handler stays
        # tiny and signal-safe.
        self._shutdown_event.clear()
        self._signal_handler_invocations = 0
        watcher = threading.Thread(
            target=self._shutdown_watcher,
            name="shutdown-watcher",
            daemon=True,
        )
        watcher.start()
        self._install_signal_handlers()

        # Blocking poll loop — exits when stop() is called.
        self._detector.run()

        # Graceful cleanup after the detector loop exits.
        if self._capture.is_recording:
            logger.info("Stopping active recording...")
            audio_path = self._capture.stop()
            if audio_path and audio_path.exists():
                duration = time.time() - self._meeting_started_at
                self._process_audio(audio_path, self._meeting_started_at, duration)

        # Wait for any in-flight background processing to complete.
        if self._processing_futures:
            logger.info(
                "Waiting for %d background processing task(s)...",
                len([f for f in self._processing_futures if not f.done()]),
            )
            for future in self._processing_futures:
                try:
                    future.result(timeout=600)
                except Exception:
                    logger.error("Processing task failed during shutdown", exc_info=True)
            self._processing_futures.clear()
        self._processing_executor.shutdown(wait=False)

        if self._api_server:
            self._api_server.stop()

    def run_record_now(self) -> None:
        """
        Skip detection and start recording immediately.
        Press Ctrl+C to stop recording and trigger processing.
        """
        logger.info("Manual recording mode. Press Ctrl+C to stop.")
        self._start_api_server()
        started_at = time.time()

        self._capture.start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        audio_path = self._capture.stop()
        duration = time.time() - started_at

        try:
            if audio_path and audio_path.exists():
                self._process_audio(audio_path, started_at, duration)
            else:
                logger.error("No audio captured.")
        finally:
            if self._api_server:
                self._api_server.stop()

    def run_process_file(self, audio_path: str) -> None:
        """
        Skip detection and capture; process an existing audio file
        directly through the transcribe -> summarise -> output pipeline.

        Raises FileNotFoundError if the audio file does not exist.
        """
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")

        self._start_api_server()
        logger.info("Processing existing file: %s", path)
        try:
            self._process_audio(path)
        finally:
            if self._api_server:
                self._api_server.stop()


def run_mic_permission_request() -> int:
    """One-off foreground mode: raise the microphone dialog and wait.

    tccd KILLS a launchd daemon for the explicit AVCaptureDevice request
    and silently zeroes the implicit one (no prompt, no TCC record) —
    but the same explicit request from a normal LaunchServices launch of
    the daemon bundle is the standard permission flow every macOS app
    uses. `open "Context Recall Daemon.app" --args
    --request-mic-permission` runs this; the grant lands on the bundle
    id, which the launchd-managed instance then inherits.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from src import mic_permission

    status = mic_permission.authorization_status()
    logger.info("microphone permission before request: %s", status)
    if status == mic_permission.AUTHORIZED:
        return 0
    granted = mic_permission.request_access(timeout_seconds=240.0)
    status = mic_permission.authorization_status()
    logger.info("request result: granted=%s, status now: %s", granted, status)
    return 0 if status == mic_permission.AUTHORIZED else 1


def main():
    parser = argparse.ArgumentParser(
        description="Context Recall: auto-detect, transcribe, and summarise Teams meetings.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--record-now",
        action="store_true",
        help="Skip meeting detection. Start recording immediately.",
    )
    parser.add_argument(
        "--process",
        type=str,
        default=None,
        help="Skip detection and capture. Process an existing audio file.",
    )
    parser.add_argument(
        "--request-mic-permission",
        action="store_true",
        help="Show the macOS microphone permission dialog and exit "
        "(run via a foreground launch of the daemon bundle).",
    )

    args = parser.parse_args()

    if args.request_mic_permission:
        sys.exit(run_mic_permission_request())

    config_path = Path(args.config) if args.config else None

    app = ContextRecall(config_path)

    try:
        if args.process:
            app.run_process_file(args.process)
        elif args.record_now:
            app.run_record_now()
        else:
            app.run_daemon()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
