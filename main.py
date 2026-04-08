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
import logging
import os
import signal
import sys
import time
from pathlib import Path

from src.audio_capture import AudioCapture
from src.detector import MeetingEvent, MeetingState, TeamsDetector
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

        # Output writers (initialised based on config).
        self._md_writer = (
            MarkdownWriter(self._config.markdown)
            if self._config.markdown.enabled
            else None
        )
        self._notion_writer = (
            NotionWriter(self._config.notion)
            if self._config.notion.enabled
            else None
        )

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
    # Detector callbacks
    # ------------------------------------------------------------------

    def _on_meeting_start(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting begins."""
        logger.info("Starting audio capture...")
        try:
            self._capture.start()
        except Exception as e:
            logger.error(f"Failed to start audio capture: {e}", exc_info=True)

    def _on_meeting_end(self, event: MeetingEvent) -> None:
        """Called by the detector when a Teams meeting ends."""
        logger.info("Stopping audio capture and processing...")
        audio_path = self._capture.stop()

        if audio_path is None or not audio_path.exists():
            logger.error("No audio file produced. Skipping processing.")
            return

        self._process_audio(
            audio_path=audio_path,
            started_at=event.started_at,
            duration_seconds=event.duration_seconds,
        )

    # ------------------------------------------------------------------
    # Processing pipeline
    # ------------------------------------------------------------------

    def _process_audio(
        self,
        audio_path: Path,
        started_at: float = 0.0,
        duration_seconds: float = 0.0,
    ) -> None:
        """
        Run the full pipeline on a captured audio file:
        transcribe -> summarise -> write outputs.
        """
        if started_at == 0.0:
            started_at = time.time()

        # Step 1: Transcribe.
        logger.info("Transcribing audio...")
        try:
            transcript = self._transcriber.transcribe(audio_path)
        except Exception as e:
            logger.error(f"Transcription failed: {e}", exc_info=True)
            return

        if transcript.word_count < 5:
            logger.warning(
                f"Transcript too short ({transcript.word_count} words). "
                f"Skipping summarisation."
            )
            return

        if duration_seconds == 0.0:
            duration_seconds = transcript.duration_seconds

        # Step 2: Summarise.
        logger.info("Generating summary...")
        try:
            summary = self._summariser.summarise(transcript)
        except Exception as e:
            logger.error(f"Summarisation failed: {e}", exc_info=True)
            return

        # Step 3: Write outputs.
        if self._md_writer:
            try:
                md_path = self._md_writer.write(
                    summary, transcript, started_at, duration_seconds
                )
                logger.info(f"Markdown output: {md_path}")
            except Exception as e:
                logger.error(f"Markdown write failed: {e}", exc_info=True)

        if self._notion_writer:
            try:
                page_url = self._notion_writer.write(
                    summary, transcript, started_at, duration_seconds
                )
                logger.info(f"Notion output: {page_url}")
            except Exception as e:
                logger.error(f"Notion write failed: {e}", exc_info=True)

        logger.info("Processing complete.")

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def run_daemon(self) -> None:
        """
        Run as a background daemon, polling for Teams meetings.
        Blocks until interrupted with SIGINT/SIGTERM.
        """
        logger.info("MeetingMind daemon starting...")

        # Handle graceful shutdown.
        def shutdown_handler(signum, frame):
            logger.info("Shutdown signal received.")
            self._detector.stop()
            if self._capture.is_recording:
                logger.info("Stopping active recording...")
                self._capture.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

        # Blocking poll loop.
        self._detector.run()

    def run_record_now(self) -> None:
        """
        Skip detection and start recording immediately.
        Press Ctrl+C to stop recording and trigger processing.
        """
        logger.info("Manual recording mode. Press Ctrl+C to stop.")
        started_at = time.time()

        self._capture.start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        audio_path = self._capture.stop()
        duration = time.time() - started_at

        if audio_path and audio_path.exists():
            self._process_audio(audio_path, started_at, duration)
        else:
            logger.error("No audio captured.")

    def run_process_file(self, audio_path: str) -> None:
        """
        Skip detection and capture; process an existing audio file
        directly through the transcribe -> summarise -> output pipeline.
        """
        path = Path(audio_path)
        if not path.exists():
            logger.error(f"File not found: {path}")
            sys.exit(1)

        logger.info(f"Processing existing file: {path}")
        self._process_audio(path)


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

    if args.process:
        app.run_process_file(args.process)
    elif args.record_now:
        app.run_record_now()
    else:
        app.run_daemon()


if __name__ == "__main__":
    main()
