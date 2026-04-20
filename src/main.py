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

from src.audio_capture import AudioCapture, AudioCaptureError
from src.detector import MeetingEvent, MeetingState, TeamsDetector
from src.diariser import EnergyDiariser, create_diariser
from src.output.markdown_writer import MarkdownWriter
from src.output.notion_writer import NotionWriter
from src.summariser import Summariser
from src.templates import TemplateManager
from src.transcriber import Transcriber
from src.utils.config import load_config

try:
    from src.calendar_matcher import CalendarMatch, CalendarMatcher
except ImportError:
    CalendarMatcher = None
    CalendarMatch = None

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

        # Background processing executor for non-blocking pipeline runs.
        self._processing_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="pipeline"
        )
        self._processing_futures: list[concurrent.futures.Future] = []

        # API server and event system (initialised lazily in run_daemon).
        self._api_server = None
        self._event_bus = None

        # Live transcription (optional).
        self._live_transcriber = None

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
                logger.warning("Calendar integration enabled but not available (check permissions)")
                self._calendar_matcher = None

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

                self._live_transcriber = LiveTranscriber(
                    model_size=self._config.transcription.model_size,
                    language=self._config.transcription.language,
                    on_segment=_on_live_segment,
                    sample_rate=self._config.audio.sample_rate,
                    config=live_config,
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
        if self._live_transcriber:
            try:
                self._live_transcriber.stop()
            except Exception:
                pass
            self._live_transcriber = None
            self._capture.on_audio_data = None

        self._emit(
            "meeting.ended",
            duration=event.duration_seconds,
        )
        try:
            audio_path = self._capture.stop(blocking=False)
        except TypeError:
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
            audio_dir = Path(os.path.expanduser("~/Library/Application Support/MeetingMind/audio"))
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
                asyncio.run_coroutine_threadsafe(
                    self._api_server.repo.update_meeting(
                        meeting_id,
                        audio_path=str(persistent_audio_path),
                    ),
                    loop,
                )
            except Exception as e:
                logger.warning("Failed to create meeting record: %s", e)

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

        # Wait for audio merge if stop was non-blocking.
        if hasattr(self._capture, "wait_for_merge"):
            if not self._capture.wait_for_merge(timeout=120):
                logger.error("Audio merge timed out after 120s. Skipping processing.")
                self._db_update(meeting_id, status="error")
                return

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

        # Step 2: Diarise (if enabled).
        if self._diariser:
            logger.info("Running speaker diarisation...")
            self._emit("pipeline.stage", meeting_id=meeting_id, stage="diarising")
            mic_path = (
                self._capture.mic_audio_path if self._config.audio.keep_source_files else None
            )
            try:
                transcript = self._diariser.diarise(transcript, audio_path, mic_audio_path=mic_path)
            except Exception as e:
                logger.error("Diarisation failed: %s", e, exc_info=True)

        # Step 2b: Enrich speaker labels from calendar attendees.
        if self._calendar_match and self._calendar_match.attendees:
            speakers = {seg.speaker for seg in transcript.segments}
            remote_label = self._config.diarisation.remote_label
            my_name = self._config.diarisation.speaker_name
            other_attendees = [
                a for a in self._calendar_match.attendees if a.get("name") and a["name"] != my_name
            ]
            # Auto-rename in 2-speaker meetings with exactly 1 other attendee.
            if len(speakers) == 2 and remote_label in speakers and len(other_attendees) == 1:
                new_name = other_attendees[0]["name"]
                for seg in transcript.segments:
                    if seg.speaker == remote_label:
                        seg.speaker = new_name
                logger.info("Speaker enrichment: renamed '%s' to '%s'", remote_label, new_name)
            # Store all attendees as candidate speaker mappings for the UI.
            if meeting_id and self._api_server and self._api_server.repo and self._api_server.loop:
                for attendee in other_attendees:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._api_server.repo.set_speaker_name(
                                meeting_id,
                                f"candidate:{attendee['name']}",
                                attendee["name"],
                                source="calendar",
                            ),
                            self._api_server.loop,
                        ).result(timeout=5)
                    except Exception:
                        pass

        # Load default template for summarisation.
        template = None
        try:
            tm = TemplateManager()
            template = tm.get_template(self._config.summarisation.default_template)
        except Exception as e:
            logger.warning("Failed to load template: %s", e)

        # Step 3: Summarise.
        logger.info("Generating summary...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="summarising")
        summary_start = time.monotonic()
        try:
            summary = self._summariser.summarise(transcript, template=template)
        except Exception as e:
            elapsed = time.monotonic() - summary_start
            logger.error("Summarisation failed after %.1fs: %s", elapsed, e, exc_info=True)
            self._emit("pipeline.error", meeting_id=meeting_id, stage="summarising", error=str(e))
            self._db_update(meeting_id, status="error")
            return

        summary_elapsed = time.monotonic() - summary_start
        logger.info("Summary generated in %.1fs", summary_elapsed)

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

        # Attach calendar data to meeting record.
        if self._calendar_match and meeting_id:
            try:
                self._db_update(
                    meeting_id,
                    calendar_event_title=self._calendar_match.event_title,
                    attendees_json=json.dumps(self._calendar_match.attendees),
                    calendar_confidence=self._calendar_match.confidence,
                    teams_join_url=self._calendar_match.teams_join_url,
                    teams_meeting_id=self._calendar_match.teams_meeting_id,
                )
            except Exception as e:
                logger.warning("Failed to save calendar data: %s", e)

        # Step 3b: Embed transcript segments for semantic search (background).
        try:
            from src.embeddings import Embedder, is_embeddings_available

            if is_embeddings_available():
                logger.info("Embedding transcript segments for search...")
                self._emit("pipeline.stage", meeting_id=meeting_id, stage="embedding")
                embedder = Embedder()
                texts = [seg.text.strip() for seg in transcript.segments if seg.text.strip()]
                if texts:
                    vectors = embedder.embed(texts)
                    emb_records = []
                    text_idx = 0
                    for i, seg in enumerate(transcript.segments):
                        if seg.text.strip():
                            emb_records.append(
                                {
                                    "segment_index": i,
                                    "embedding": vectors[text_idx],
                                    "text": seg.text.strip(),
                                    "speaker": seg.speaker,
                                    "start_time": seg.start,
                                }
                            )
                            text_idx += 1

                    if meeting_id and self._api_server and self._api_server.repo:
                        loop = self._api_server.loop
                        if loop and not loop.is_closed():
                            future = asyncio.run_coroutine_threadsafe(
                                self._api_server.repo.store_embeddings(meeting_id, emb_records),
                                loop,
                            )

                            emb_count = len(emb_records)

                            def _on_embedding_done(fut):
                                try:
                                    fut.result()
                                    logger.info("Stored %d segment embeddings", emb_count)
                                except Exception as exc:
                                    logger.warning("Embedding storage failed: %s", exc)

                            future.add_done_callback(_on_embedding_done)
        except Exception as e:
            logger.warning("Embedding failed (search will still work without it): %s", e)

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

        # Post-processing: intelligence features (non-fatal).
        self._run_post_processing(meeting_id, transcript)

        self._active_meeting_id = None
        logger.info("Processing complete.")

    def _run_post_processing(self, meeting_id: str | None, transcript) -> None:
        """Run intelligence post-processing after pipeline completes. Non-fatal."""
        if not meeting_id or not self._api_server or not self._api_server.repo:
            return
        loop = self._api_server.loop
        if not loop or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._post_process_async(meeting_id, transcript), loop)
        except Exception:
            logger.warning("Post-processing dispatch failed", exc_info=True)

    async def _post_process_async(self, meeting_id: str, transcript) -> None:
        """Async post-processing: action items, analytics."""
        try:
            if self._config.action_items.auto_extract:
                await self._extract_action_items(meeting_id, transcript)
        except Exception:
            logger.warning("Action item extraction failed", exc_info=True)
        try:
            await self._refresh_analytics()
        except Exception:
            logger.warning("Analytics refresh failed", exc_info=True)

    async def _extract_action_items(self, meeting_id: str, transcript) -> None:
        """Extract and store action items from transcript."""
        from src.action_items.extractor import ActionItemExtractor
        from src.action_items.repository import ActionItemRepository

        extractor = ActionItemExtractor(
            summarisation_config=self._config.summarisation,
            config=self._config.action_items,
        )
        items = extractor.extract(transcript)
        if not items:
            return
        ai_repo = ActionItemRepository(self._api_server.db)
        for item in items:
            await ai_repo.create(
                meeting_id=meeting_id,
                title=item["title"],
                assignee=item.get("assignee"),
                due_date=item.get("due_date"),
                priority=item.get("priority", "medium"),
                source="extracted",
                extracted_text=item.get("extracted_text"),
            )
        logger.info("Extracted %d action items from meeting %s", len(items), meeting_id)
        self._emit("action_items.extracted", meeting_id=meeting_id, count=len(items))

    async def _refresh_analytics(self) -> None:
        """Refresh current day analytics after meeting completes."""
        from datetime import datetime, timezone

        from src.action_items.repository import ActionItemRepository
        from src.analytics.engine import AnalyticsEngine
        from src.analytics.repository import AnalyticsRepository

        analytics_repo = AnalyticsRepository(self._api_server.db)
        ai_repo = ActionItemRepository(self._api_server.db)
        engine = AnalyticsEngine(
            config=self._config.analytics,
            meeting_repo=self._api_server.repo,
            analytics_repo=analytics_repo,
            action_item_repo=ai_repo,
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await engine.refresh_period("daily", today)

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
        except Exception:
            logger.error("API recording start failed", exc_info=True)
            self._emit("pipeline.error", stage="capture", error="Failed to start audio capture")
            raise
        self._meeting_started_at = time.time()
        self._emit("meeting.started", started_at=self._meeting_started_at)

    def api_stop_recording(self) -> None:
        """Stop a manual recording and trigger background processing."""
        started_at = self._meeting_started_at
        self._emit("meeting.ended", duration=time.time() - started_at)
        audio_path = self._capture.stop()

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

        if not audio_path or not audio_path.exists():
            raise AudioCaptureError("No audio file produced")

        _, meeting_id = self._persist_audio(audio_path, started_at, status="pending")
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
