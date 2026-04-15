"""
MeetingMind — main entry point and orchestrator.

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
import os
import shutil
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from src.audio_capture import AudioCapture
from src.detector import MeetingEvent, MeetingState, TeamsDetector
from src.diariser import Diariser
from src.output.markdown_writer import MarkdownWriter
from src.output.notion_writer import NotionWriter
from src.summariser import Summariser
from src.transcriber import Transcriber
from src.utils.config import load_config

logger = logging.getLogger("meetingmind")


class MeetingMind:
    """
    Top-level orchestrator. Connects the detector's callbacks to
    the recording pipeline and manages the lifecycle of each
    meeting session.
    """

    def __init__(self, config_path: Path | None = None):
        self._config = load_config(config_path)
        self._setup_logging()

        self._detector = TeamsDetector(self._config.detection)
        self._capture = AudioCapture(self._config.audio)
        self._transcriber = Transcriber(self._config.transcription)
        self._summariser = Summariser(self._config.summarisation)
        self._diariser = (
            Diariser(self._config.diarisation) if self._config.diarisation.enabled else None
        )

        # If diarisation is enabled, keep source files for comparison.
        if self._diariser:
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

        # Background processing executor for non-blocking pipeline runs.
        self._processing_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pipeline"
        )
        self._processing_futures: list[concurrent.futures.Future] = []

        # API server and event system (initialised lazily in run_daemon).
        self._api_server = None
        self._event_bus = None

        # Wire up detector callbacks.
        self._detector.on_meeting_start = self._on_meeting_start
        self._detector.on_meeting_end = self._on_meeting_end

    def _setup_logging(self) -> None:
        """Configure logging to both console and file."""
        log_level = getattr(logging, self._config.logging.level.upper(), logging.INFO)
        log_file = self._config.logging.log_file

        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler(log_file, encoding="utf-8"),
            ],
        )

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

    def _on_meeting_start(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting begins."""
        logger.info("Starting audio capture...")

        # Wire audio level callback for live metering.
        def _on_level(system_rms: float, mic_rms: float) -> None:
            self._emit(
                "audio.level",
                system_rms=round(system_rms, 6),
                mic_rms=round(mic_rms, 6),
            )

        self._capture.on_audio_level = _on_level

        try:
            self._capture.start()
        except Exception as e:
            logger.error("Failed to start audio capture: %s", e, exc_info=True)
            self._emit("pipeline.error", stage="capture", error=str(e))
            return

        # Only update state after capture has started successfully.
        self._meeting_started_at = event.started_at or time.time()
        self._emit("meeting.started", started_at=self._meeting_started_at)

    def _on_meeting_end(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting ends."""
        logger.info("Stopping audio capture and processing...")
        self._emit(
            "meeting.ended",
            duration=event.duration_seconds,
        )
        audio_path = self._capture.stop()

        if audio_path is None or not audio_path.exists():
            logger.error("No audio file produced. Skipping processing.")
            return

        # Capture meeting_id before clearing so background thread has it.
        meeting_id = self._active_meeting_id

        # Clear active meeting ID so the next meeting can start fresh.
        self._active_meeting_id = None

        # Remove completed futures.
        self._processing_futures = [f for f in self._processing_futures if not f.done()]

        future = self._processing_executor.submit(
            self._process_audio,
            audio_path=audio_path,
            started_at=event.started_at,
            duration_seconds=event.duration_seconds,
            meeting_id=meeting_id,
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

    # ------------------------------------------------------------------
    # Processing pipeline
    # ------------------------------------------------------------------

    def _process_audio(
        self,
        audio_path: Path,
        started_at: float = 0.0,
        duration_seconds: float = 0.0,
        meeting_id: str | None = None,
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

        # Persist audio to a durable location if the API server is running.
        persistent_audio_path = audio_path
        if self._api_server and self._api_server.repo:
            audio_dir = Path(os.path.expanduser("~/Library/Application Support/MeetingMind/audio"))
            audio_dir.mkdir(parents=True, exist_ok=True)
            persistent_audio_path = audio_dir / audio_path.name
            if audio_path != persistent_audio_path:
                try:
                    os.link(audio_path, persistent_audio_path)
                except OSError:
                    shutil.copy2(audio_path, persistent_audio_path)

        # Create meeting record in DB.
        if self._api_server and self._api_server.repo and self._api_server.loop:
            loop = self._api_server.loop
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._api_server.repo.create_meeting(
                        started_at=started_at, status="transcribing"
                    ),
                    loop,
                )
                meeting_id = future.result(timeout=5)
                self._active_meeting_id = meeting_id
                # Update with audio path.
                asyncio.run_coroutine_threadsafe(
                    self._api_server.repo.update_meeting(
                        meeting_id,
                        audio_path=str(persistent_audio_path),
                    ),
                    loop,
                )
            except Exception as e:
                logger.warning("Failed to create meeting record: %s", e)

        # Step 1: Transcribe.
        logger.info("Transcribing audio...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="transcribing")

        def on_segment(seg):
            self._emit(
                "transcript.segment",
                meeting_id=meeting_id,
                segment=asdict(seg),
            )

        try:
            transcript = self._transcriber.transcribe(audio_path, on_segment=on_segment)
        except Exception as e:
            logger.error("Transcription failed: %s", e, exc_info=True)
            self._emit("pipeline.error", meeting_id=meeting_id, stage="transcribing", error=str(e))
            self._db_update(meeting_id, status="error")
            return

        if transcript.word_count < 5:
            logger.warning(
                "Transcript too short (%d words). Skipping summarisation.",
                transcript.word_count,
            )
            self._db_update(meeting_id, status="error")
            return

        if duration_seconds == 0.0:
            duration_seconds = transcript.duration_seconds

        # Step 2: Diarise (if enabled and source files are available).
        if self._diariser:
            sys_path = self._capture.system_audio_path
            mic_path = self._capture.mic_audio_path
            if sys_path and mic_path:
                logger.info("Running speaker diarisation...")
                self._emit("pipeline.stage", meeting_id=meeting_id, stage="diarising")
                try:
                    transcript = self._diariser.diarise(transcript, sys_path, mic_path)
                except Exception as e:
                    logger.error("Diarisation failed: %s", e, exc_info=True)

        # Step 3: Summarise.
        logger.info("Generating summary...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="summarising")
        try:
            summary = self._summariser.summarise(transcript)
        except Exception as e:
            logger.error("Summarisation failed: %s", e, exc_info=True)
            self._emit("pipeline.error", meeting_id=meeting_id, stage="summarising", error=str(e))
            self._db_update(meeting_id, status="error")
            return

        # Persist transcript and summary to DB.
        self._db_update(
            meeting_id,
            title=summary.title or "Untitled Meeting",
            ended_at=started_at + duration_seconds,
            duration_seconds=duration_seconds,
            status="complete",
            transcript_json=json.dumps(transcript.to_dict()),
            summary_markdown=summary.raw_markdown,
            tags=summary.tags,
            language=transcript.language,
            word_count=transcript.word_count,
        )

        # Step 4: Write outputs.
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="writing")

        if self._md_writer:
            try:
                md_path = self._md_writer.write(summary, transcript, started_at, duration_seconds)
                logger.info("Markdown output: %s", md_path)
            except Exception as e:
                logger.error("Markdown write failed: %s", e, exc_info=True)

        if self._notion_writer:
            try:
                page_url = self._notion_writer.write(
                    summary, transcript, started_at, duration_seconds
                )
                logger.info("Notion output: %s", page_url)
            except Exception as e:
                logger.error("Notion write failed: %s", e, exc_info=True)

        self._emit("pipeline.complete", meeting_id=meeting_id, title=summary.title)
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

        Raises AudioCaptureError if the audio device cannot be opened.
        """
        try:
            self._capture.start()
        except Exception:
            logger.error("API recording start failed", exc_info=True)
            self._emit("pipeline.error", stage="capture", error="Failed to start audio capture")
            raise
        self._meeting_started_at = time.time()
        self._emit("meeting.started", started_at=self._meeting_started_at)

    def api_stop_recording(self) -> None:
        """Stop a manual recording and trigger processing. Runs synchronously."""
        started_at = self._meeting_started_at
        self._emit("meeting.ended", duration=time.time() - started_at)
        audio_path = self._capture.stop()

        if audio_path and audio_path.exists():
            duration = time.time() - started_at
            self._process_audio(audio_path, started_at, duration)

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
            is_recording=lambda: self._capture.is_recording,
        )
        self._api_server.start()

        # Give the server a moment to bind.
        time.sleep(0.5)

    def run_daemon(self) -> None:
        """
        Run as a background daemon, polling for Teams meetings.
        Blocks until interrupted with SIGINT/SIGTERM.
        """
        logger.info("MeetingMind daemon starting...")

        # Start the API server for UI communication.
        self._start_api_server()

        # Handle graceful shutdown — signal handler only sets a flag;
        # heavy cleanup runs on the main thread after the poll loop exits.
        def shutdown_handler(signum, frame):
            logger.info("Shutdown signal received.")
            self._detector.stop()

        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

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


def main():
    parser = argparse.ArgumentParser(
        description="MeetingMind: auto-detect, transcribe, and summarise Teams meetings.",
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

    args = parser.parse_args()
    config_path = Path(args.config) if args.config else None

    app = MeetingMind(config_path)

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
