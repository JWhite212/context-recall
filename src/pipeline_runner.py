"""
Shared post-capture processing pipeline.

Both entry points that turn an audio file into a finished meeting —
the live orchestrator (``src/main.py``) and the reprocess API route
(``src/api/routes/reprocess.py``) — drive the same ``PipelineRunner``.
Before this module existed the reprocess route re-implemented a subset
of the orchestrator's pipeline and drifted (no diarisation, no output
writers, no embeddings, no post-processing); centralising the stages
makes that class of gap structurally impossible.

The runner is synchronous and designed to run on a worker thread (the
orchestrator's ThreadPoolExecutor, or ``asyncio.to_thread`` from the
API loop). Database access goes through :class:`DbBridge`, which
marshals coroutines onto the API server's event loop the same way the
orchestrator always has.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from src.diariser import EnergyDiariser, create_diariser
from src.output.markdown_writer import MarkdownWriter
from src.output.notion_writer import NotionWriter
from src.summariser import Summariser
from src.template_selection import TemplateSelector
from src.templates import TemplateManager
from src.transcriber import Transcriber

logger = logging.getLogger("contextrecall.pipeline")

SHORT_TRANSCRIPT_TITLE = "Untitled Meeting (short)"
EMPTY_TRANSCRIPT_ERROR = "Transcript is empty. The audio may be silent or corrupted."
MIN_SUMMARISABLE_WORDS = 5


class DbBridge:
    """Thread-safe database access for pipeline threads.

    Wraps the API server's repository + event loop so synchronous
    pipeline code can persist results without owning an event loop.
    A bridge whose loop is gone reports ``available == False`` and
    every operation degrades to a logged no-op — mirroring the
    orchestrator's long-standing ``_api_server is None`` short-circuit.
    """

    def __init__(self, repo, loop, database=None) -> None:
        self._repo = repo
        self._loop = loop
        self._database = database

    @property
    def available(self) -> bool:
        return self._repo is not None and self._loop is not None and not self._loop.is_closed()

    @property
    def repo(self):
        return self._repo

    @property
    def database(self):
        return self._database

    def update_meeting(self, meeting_id: str | None, **fields) -> None:
        """Fire-and-forget meeting update with loud failure logging (C3)."""
        if not meeting_id or self._repo is None:
            return
        self._submit_write(
            lambda: self._repo.update_meeting(meeting_id, **fields),
            meeting_id,
            sorted(fields.keys()),
        )

    def update_title_if_auto(self, meeting_id: str | None, title: str) -> None:
        """Fire-and-forget conditional auto-title write (I4).

        Delegates to the repository's atomic
        ``UPDATE ... WHERE title_source != 'manual'`` so a rename issued
        while the pipeline is in flight always wins over the auto-title.
        """
        if not meeting_id or self._repo is None:
            return
        self._submit_write(
            lambda: self._repo.update_title_if_auto(meeting_id, title),
            meeting_id,
            ["title (if auto)", "title_source"],
        )

    def _submit_write(self, make_coro, meeting_id: str, fields_desc: list[str]) -> None:
        """Schedule a repo write on the API loop; log loudly if dropped.

        ``make_coro`` is a zero-arg factory so no coroutine is ever created
        (and left un-awaited) when the loop is already gone.
        """
        if self._loop is None or self._loop.is_closed():
            logger.error(
                "DB update for meeting %s dropped: event loop is %s. Fields lost: %s",
                meeting_id,
                "closed" if self._loop and self._loop.is_closed() else "missing",
                fields_desc,
            )
            return
        coro = make_coro()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            coro.close()
            logger.error(
                "Failed to schedule DB update for meeting %s",
                meeting_id,
                exc_info=True,
            )
            return

        def _log_db_error(fut):
            exc = fut.exception()
            if exc:
                logger.error("DB update failed for meeting %s: %s", meeting_id, exc)

        future.add_done_callback(_log_db_error)

    def try_call(self, coro, timeout: float = 15.0, what: str = "db call"):
        """Run *coro* on the API loop and block for its result.

        Returns the result, or ``None`` if the bridge is unavailable or
        the call failed (logged, never raised — pipeline stages treat
        persistence problems as non-fatal).
        """
        if not self.available:
            coro.close()
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            coro.close()
            logger.warning("Failed to schedule %s", what, exc_info=True)
            return None
        try:
            return future.result(timeout=timeout)
        except TimeoutError as e:
            # Stop the coroutine on the loop too — a zombie DB write
            # landing later would race whatever the caller does next.
            future.cancel()
            logger.warning("%s timed out after %.0fs: %r", what, timeout, e)
            return None
        except Exception as e:
            logger.warning("%s failed: %r", what, e)
            return None

    def schedule(self, coro, what: str = "background task") -> None:
        """Fire-and-forget a coroutine onto the API loop."""
        if not self.available:
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            coro.close()
            logger.warning("Failed to schedule %s", what, exc_info=True)


@dataclass
class RunResult:
    """Outcome of a pipeline run."""

    status: str  # "complete" | "short" | "error"
    title: str | None = None
    error: str | None = None


class PipelineRunner:
    """Runs transcribe → diarise → summarise → persist → write → post-process."""

    def __init__(
        self,
        config,
        *,
        emit=None,
        db: DbBridge | None = None,
        transcriber: Transcriber | None = None,
        summariser: Summariser | None = None,
        diariser=None,
        md_writer: MarkdownWriter | None = None,
        notion_writer: NotionWriter | None = None,
    ) -> None:
        self._config = config
        self._emit_cb = emit
        self._db = db
        self._transcriber = transcriber or Transcriber(config.transcription)
        self._summariser = summariser or Summariser(config.summarisation)
        self._diariser = diariser
        self._md_writer = md_writer
        self._notion_writer = notion_writer

    @classmethod
    def from_config(cls, config, *, emit=None, db: DbBridge | None = None) -> "PipelineRunner":
        """Build a runner with components constructed from *config*.

        Used by the reprocess route, which loads config fresh per request
        so settings changes apply without a daemon restart.
        """
        diariser = create_diariser(config.diarisation) if config.diarisation.enabled else None
        md_writer = MarkdownWriter(config.markdown) if config.markdown.enabled else None
        notion_writer = NotionWriter(config.notion) if config.notion.enabled else None
        return cls(
            config,
            emit=emit,
            db=db,
            diariser=diariser,
            md_writer=md_writer,
            notion_writer=notion_writer,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, **kwargs) -> None:
        if self._emit_cb:
            self._emit_cb(event_type, **kwargs)

    def _db_available(self) -> bool:
        return self._db is not None and self._db.available

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def run(
        self,
        audio_path: Path,
        meeting_id: str | None,
        started_at: float,
        duration_seconds: float = 0.0,
        *,
        attendees: list[dict] | None = None,
        mic_audio_path: Path | None = None,
        preserve_mappings: bool = False,
        notion_page_id: str | None = None,
        calendar_fields: dict | None = None,
        is_reprocess: bool = False,
        preserve_title: bool = False,
    ) -> RunResult:
        """Run the post-capture pipeline over *audio_path*.

        Args:
            attendees: calendar attendees (``[{"name", "email"}]``) used
                for speaker enrichment. Live runs pass the fresh calendar
                match; reprocess passes the stored ``attendees_json``.
            mic_audio_path: separate mic source WAV for the energy
                diariser, when it still exists.
            preserve_mappings: re-apply stored speaker renames to the
                fresh transcript (reprocess only — a re-run must not
                undo the user's manual speaker labelling).
            notion_page_id: previously written Notion page; it is
                archived before the replacement page is created so
                reprocessing never accumulates duplicates.
            calendar_fields: extra meeting columns persisted after the
                summary (live runs attach calendar match data here).
            is_reprocess: replace previously *extracted* action items
                instead of appending duplicates.
            preserve_title: skip writing title/title_source — the user
                manually renamed this meeting (title_source == 'manual'),
                and a re-run must not revert it.
        """
        # Step 1: Transcribe.
        logger.info("Transcribing audio...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="transcribing")

        def on_segment(seg):
            from dataclasses import asdict

            self._emit("transcript.segment", meeting_id=meeting_id, segment=asdict(seg))

        try:
            transcript = self._transcriber.transcribe(audio_path, on_segment=on_segment)
        except Exception as e:
            logger.error("Transcription failed: %s", e, exc_info=True)
            self._emit("pipeline.error", meeting_id=meeting_id, stage="transcribing", error=str(e))
            self._update(meeting_id, status="error")
            return RunResult("error", error=str(e))

        if not transcript.segments:
            logger.warning("Transcript is empty — marking meeting as error.")
            self._emit(
                "pipeline.error",
                meeting_id=meeting_id,
                stage="transcribing",
                error=EMPTY_TRANSCRIPT_ERROR,
            )
            self._update(meeting_id, status="error")
            return RunResult("error", error=EMPTY_TRANSCRIPT_ERROR)

        if duration_seconds == 0.0:
            duration_seconds = transcript.duration_seconds

        # Short-but-real transcript: persist without summarisation (B1).
        if transcript.word_count < MIN_SUMMARISABLE_WORDS:
            logger.warning(
                "Transcript too short (%d words). Persisting without summarisation.",
                transcript.word_count,
            )
            self._update(
                meeting_id,
                ended_at=started_at + duration_seconds,
                duration_seconds=duration_seconds,
                status="complete",
                transcript_json=json.dumps(transcript.to_dict()),
                # A reprocess that now yields a near-empty transcript must
                # not leave the previous run's summary/tags looking current.
                summary_markdown=None,
                tags=[],
                language=transcript.language,
                word_count=transcript.word_count,
            )
            # Title precedence is enforced at write time (I3/I4): the
            # conditional write never clobbers a manual rename, and
            # preserve_title skips the write entirely on reprocess.
            if not preserve_title:
                short_title = (calendar_fields or {}).get(
                    "calendar_event_title"
                ) or SHORT_TRANSCRIPT_TITLE
                self._update_title_if_auto(meeting_id, short_title)
            self._update_fts(meeting_id)
            # is_reprocess + started_at let the UI key its pending live
            # rename to the exact recording session (C1).
            self._emit(
                "pipeline.complete",
                meeting_id=meeting_id,
                title=SHORT_TRANSCRIPT_TITLE,
                is_reprocess=is_reprocess,
                started_at=started_at,
            )
            return RunResult("short", title=SHORT_TRANSCRIPT_TITLE)

        # Step 2: Diarise.
        self._diarise(transcript, audio_path, mic_audio_path, meeting_id)

        # Step 2b: Re-apply stored speaker renames (reprocess only).
        if preserve_mappings:
            self._reapply_speaker_mappings(transcript, meeting_id)

        # Step 2c: Voice identification against enrolled people profiles.
        self._identify_voices(transcript, audio_path, meeting_id)

        # Step 2d: Enrich speaker labels from calendar attendees.
        self._enrich_speakers_from_attendees(transcript, attendees or [], meeting_id)

        # Step 2e: Client/project pre-assignment. Deterministic signals
        # (attendee email domains, calendar-title aliases, series
        # inheritance) resolve before summarisation so the matched
        # client/project descriptions can steer the summary.
        extra_context = self._resolve_assignment_context(
            meeting_id, attendees or [], calendar_fields
        )

        # Step 3: Summarise (per-meeting template: manual -> auto -> default).
        template, template_source = self._select_template_with_source(
            meeting_id, transcript, attendees or [], calendar_fields
        )

        logger.info("Generating summary...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="summarising")
        import time as _time

        summary_start = _time.monotonic()
        try:
            summary = self._summariser.summarise(
                transcript, template=template, extra_context=extra_context
            )
        except Exception as e:
            elapsed = _time.monotonic() - summary_start
            logger.error("Summarisation failed after %.1fs: %s", elapsed, e, exc_info=True)
            self._emit("pipeline.error", meeting_id=meeting_id, stage="summarising", error=str(e))
            self._update(meeting_id, status="error")
            return RunResult("error", error=str(e))
        logger.info("Summary generated in %.1fs", _time.monotonic() - summary_start)

        # Step 4: Persist transcript + summary, then refresh FTS.
        persist_fields = dict(
            ended_at=started_at + duration_seconds,
            duration_seconds=duration_seconds,
            status="complete",
            transcript_json=json.dumps(transcript.to_dict()),
            summary_markdown=summary.raw_markdown,
            tags=summary.tags,
            language=transcript.language,
            word_count=transcript.word_count,
            template_name=template.name if template else "",
            template_source=template_source,
        )
        self._update(meeting_id, **persist_fields)
        if calendar_fields and meeting_id:
            try:
                self._update(meeting_id, **calendar_fields)
            except Exception as e:
                logger.warning("Failed to save calendar data: %s", e)
        # Title precedence is enforced at write time, not via a start-of-run
        # snapshot (I4): the conditional UPDATE ... WHERE title_source !=
        # 'manual' means a rename issued while this pipeline was in flight
        # is never clobbered. preserve_title stays as the reprocess-time
        # fast skip for meetings already known to be manually titled.
        if not preserve_title:
            auto_title = (
                (calendar_fields or {}).get("calendar_event_title")
                or summary.title
                or "Untitled Meeting"
            )
            self._update_title_if_auto(meeting_id, auto_title)
        self._update_fts(meeting_id)

        # Step 5: Embed transcript segments for semantic search.
        self._embed_segments(transcript, meeting_id)

        # Step 6: Write outputs. On a reprocess of a manually-renamed
        # meeting (preserve_title) the fresh note/page must carry the
        # PRESERVED meeting title, not summary.title — the writer derives
        # the .md filename and frontmatter from the summary title, and a
        # summary-titled rewrite would repoint markdown_path and orphan
        # the manually-renamed note (I6).
        output_summary = summary
        if preserve_title and meeting_id and self._db_available():
            preserved = self._db.try_call(
                self._db.repo.get_meeting(meeting_id), what="fetch preserved title"
            )
            preserved_title = getattr(preserved, "title", "") or ""
            if preserved_title and preserved_title != summary.title:
                output_summary = replace(summary, title=preserved_title)
        self._write_outputs(
            output_summary, transcript, started_at, duration_seconds, meeting_id, notion_page_id
        )

        # is_reprocess + started_at let the UI key its pending live rename
        # to the exact recording session (C1).
        self._emit(
            "pipeline.complete",
            meeting_id=meeting_id,
            title=output_summary.title,
            is_reprocess=is_reprocess,
            started_at=started_at,
        )

        # Step 7: Intelligence post-processing (non-fatal, async).
        self._dispatch_post_processing(meeting_id, transcript, started_at, is_reprocess)

        return RunResult("complete", title=output_summary.title)

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _update(self, meeting_id: str | None, **fields) -> None:
        if self._db is not None:
            self._db.update_meeting(meeting_id, **fields)

    def _update_title_if_auto(self, meeting_id: str | None, title: str) -> None:
        if self._db is not None:
            self._db.update_title_if_auto(meeting_id, title)

    def _update_fts(self, meeting_id: str | None) -> None:
        if meeting_id and self._db_available():
            self._db.try_call(self._db.repo.update_fts(meeting_id), timeout=15.0, what="FTS update")

    def _diarise(
        self,
        transcript,
        audio_path: Path,
        mic_audio_path: Path | None,
        meeting_id: str | None,
    ) -> None:
        if not self._diariser:
            return
        logger.info("Running speaker diarisation...")
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="diarising")
        try:
            if isinstance(self._diariser, EnergyDiariser):
                self._diariser.diarise(transcript, audio_path, mic_audio_path=mic_audio_path)
            else:
                # PyAnnote works from the combined file and takes no
                # mic path (passing one was a latent TypeError).
                self._diariser.diarise(transcript, audio_path)
        except Exception as e:
            logger.error("Diarisation failed: %s", e, exc_info=True)
            self._emit(
                "pipeline.warning",
                meeting_id=meeting_id,
                source="diarisation",
                message=f"Diarisation skipped: {e}",
            )

    def _reapply_speaker_mappings(self, transcript, meeting_id: str | None) -> None:
        """Re-apply stored speaker renames to a freshly generated transcript.

        Without this, reprocessing regenerates the transcript with raw
        diariser labels and silently discards every rename the user made.
        """
        if not meeting_id or not self._db_available():
            return
        mappings = self._db.try_call(
            self._db.repo.get_speaker_names(meeting_id),
            timeout=10.0,
            what="speaker mappings fetch",
        )
        if not mappings:
            return
        for mapping in mappings:
            speaker_id = mapping.get("speaker_id", "")
            display_name = mapping.get("display_name", "")
            if not speaker_id or not display_name or speaker_id.startswith("candidate:"):
                continue
            for seg in transcript.segments:
                if seg.speaker == speaker_id:
                    seg.speaker = display_name

    def _identify_voices(self, transcript, audio_path: Path, meeting_id: str | None) -> None:
        """Match still-unresolved speakers against enrolled voice profiles.

        Runs after stored renames (a manual label always wins) and before
        attendee enrichment (a voice match is stronger evidence than "the
        calendar says one other person was invited"). Non-fatal.
        """
        cfg = getattr(self._config, "voice_id", None)
        if not cfg or not cfg.enabled:
            return
        if not self._db_available() or self._db.database is None:
            return
        try:
            from src.people.repository import PersonRepository
            from src.voice.embedder import VoiceEmbedder, is_voice_id_available
            from src.voice.recognition import VoiceRecogniser

            if not is_voice_id_available():
                return
            person_repo = PersonRepository(self._db.database)
            profiles = self._db.try_call(
                person_repo.get_all_voice_profiles(),
                timeout=10.0,
                what="voice profiles fetch",
            )
            if not profiles:
                return
            logger.info("Running voice identification (%d profile samples)...", len(profiles))

            # The recogniser needs the diarisation labels + voice_id knobs.
            class _RecogniserConfig:
                remote_label = self._config.diarisation.remote_label
                match_threshold = cfg.match_threshold
                cluster_threshold = cfg.cluster_threshold
                min_segment_seconds = cfg.min_segment_seconds
                split_unmatched_speakers = cfg.split_unmatched_speakers

            recogniser = VoiceRecogniser(VoiceEmbedder(cfg.model_source), _RecogniserConfig())
            matches = recogniser.identify(transcript, audio_path, profiles)
            # speaker_mappings holds ONE row per (meeting, label). When two
            # different people were matched inside the same original label,
            # storing either would corrupt re-application on reprocess —
            # the transcript renames stand, but the mapping is skipped.
            person_matches = [m for m in matches if m.person_id]
            by_label: dict[str, list] = {}
            for match in person_matches:
                by_label.setdefault(match.original_label, []).append(match)
            for label, label_matches in by_label.items():
                if not meeting_id:
                    continue
                if len({m.person_id for m in label_matches}) > 1:
                    logger.info(
                        "Label '%s' matched multiple people; skipping mapping row",
                        label,
                    )
                    continue
                match = label_matches[0]
                self._db.try_call(
                    self._db.repo.set_speaker_name(
                        meeting_id,
                        match.original_label,
                        match.new_label,
                        source="voice",
                        person_id=match.person_id,
                        confidence=match.confidence,
                    ),
                    timeout=5.0,
                    what="voice speaker mapping",
                )
        except Exception as e:
            logger.warning("Voice identification failed (continuing without): %s", e)

    def _enrich_speakers_from_attendees(
        self, transcript, attendees: list[dict], meeting_id: str | None
    ) -> None:
        if not attendees:
            return
        speakers = {seg.speaker for seg in transcript.segments}
        remote_label = self._config.diarisation.remote_label
        my_name = self._config.diarisation.speaker_name
        other_attendees = [a for a in attendees if a.get("name") and a["name"] != my_name]
        # Auto-rename in 2-speaker meetings with exactly 1 other attendee.
        if len(speakers) == 2 and remote_label in speakers and len(other_attendees) == 1:
            new_name = other_attendees[0]["name"]
            for seg in transcript.segments:
                if seg.speaker == remote_label:
                    seg.speaker = new_name
            logger.info("Speaker enrichment: renamed '%s' to '%s'", remote_label, new_name)
        # Store all attendees as candidate speaker mappings for the UI.
        if meeting_id and self._db_available():
            for attendee in other_attendees:
                self._db.try_call(
                    self._db.repo.set_speaker_name(
                        meeting_id,
                        f"candidate:{attendee['name']}",
                        attendee["name"],
                        source="calendar",
                    ),
                    timeout=5.0,
                    what="candidate speaker mapping",
                )

    def _select_template_with_source(self, meeting_id, transcript, attendees, calendar_fields):
        """Resolve ``(SummaryTemplate, source)`` for a meeting.

        Precedence: a persisted manual override -> LLM auto-selection ->
        the configured default. Runs before summarisation, so the selector
        sees the title/attendees/transcript (never the summary). Any failure
        falls back to the default template; selection never fails the run.
        """
        sm = self._config.summarisation
        tm = TemplateManager()
        default_name = sm.default_template

        meeting = None
        if meeting_id and self._db_available():
            meeting = self._db.try_call(
                self._db.repo.get_meeting(meeting_id),
                timeout=10.0,
                what="meeting fetch for template selection",
            )

        # 1. A manual override persisted on the meeting always wins.
        if meeting is not None and getattr(meeting, "template_source", "") == "manual":
            manual_name = getattr(meeting, "template_name", "") or ""
            manual_tpl = tm.get_template(manual_name) if manual_name else None
            if manual_tpl:
                return manual_tpl, "manual"

        # 2. LLM auto-selection (best effort).
        if sm.auto_select_template:
            calendar_title = (calendar_fields or {}).get("calendar_event_title") or (
                getattr(meeting, "calendar_event_title", "") if meeting else ""
            )
            title = calendar_title or (getattr(meeting, "title", "") if meeting else "") or ""
            chosen = default_name
            try:
                chosen = TemplateSelector(sm).select(
                    title=title,
                    attendees=attendees or [],
                    transcript_text=transcript.full_text,
                    templates=tm.list_templates(),
                    default_name=default_name,
                    min_confidence=sm.template_select_min_confidence,
                )
            except Exception as e:
                logger.warning("Template selection failed: %s", e)
            tpl = tm.get_template(chosen)
            if tpl:
                return tpl, ("auto" if chosen != default_name else "default")

        # 3. Default.
        return tm.get_template(default_name), "default"

    def _resolve_assignment_context(
        self,
        meeting_id: str | None,
        attendees: list[dict],
        calendar_fields: dict | None,
    ) -> str | None:
        """Deterministic client/project pre-assignment + summariser context.

        Non-fatal; returns the fenced context text (or None). A manual
        assignment is never overwritten, but its descriptions still feed
        the summariser.
        """
        cfg = getattr(self._config, "tagging", None)
        if not cfg or not cfg.enabled:
            return None
        if not self._db_available() or self._db.database is None:
            return None
        try:
            from src.tagging.assigner import (
                Assignment,
                build_context_text,
                deterministic_assignment,
            )
            from src.tagging.repository import ClientProjectRepository

            cp_repo = ClientProjectRepository(self._db.database)
            roster = self._db.try_call(cp_repo.roster(), timeout=10.0, what="client roster fetch")
            if not roster or (not roster["clients"] and not roster["projects"]):
                return None

            meeting = None
            if meeting_id:
                meeting = self._db.try_call(
                    self._db.repo.get_meeting(meeting_id),
                    timeout=10.0,
                    what="meeting fetch for assignment",
                )

            if meeting is not None and getattr(meeting, "assignment_source", "") == "manual":
                assignment = Assignment(
                    client_id=meeting.client_id,
                    project_id=meeting.project_id,
                    confidence=1.0,
                    method="manual",
                )
            else:
                series_assignment = None
                series_id = getattr(meeting, "series_id", None) if meeting else None
                if series_id:
                    series_assignment = self._db.try_call(
                        cp_repo.latest_assignment_for_series(series_id),
                        timeout=10.0,
                        what="series assignment fetch",
                    )
                calendar_title = (calendar_fields or {}).get("calendar_event_title") or (
                    getattr(meeting, "calendar_event_title", "") if meeting else ""
                )
                assignment = deterministic_assignment(
                    roster,
                    attendees=attendees,
                    calendar_title=calendar_title or "",
                    series_assignment=series_assignment,
                )
                if assignment and meeting_id:
                    self._update(
                        meeting_id,
                        client_id=assignment.client_id,
                        project_id=assignment.project_id,
                        assignment_source="auto",
                        assignment_confidence=assignment.confidence,
                    )
                    logger.info(
                        "Assigned meeting %s to client=%s project=%s (%s, %.2f)",
                        meeting_id,
                        assignment.client_id,
                        assignment.project_id,
                        assignment.method,
                        assignment.confidence,
                    )

            if assignment is None or not cfg.inject_context:
                return None
            return build_context_text(roster, assignment, cfg.max_context_chars)
        except Exception as e:
            logger.warning("Client/project pre-assignment failed: %s", e)
            return None

    def _embed_segments(self, transcript, meeting_id: str | None) -> None:
        try:
            from src.embeddings import Embedder, is_embeddings_available

            if not is_embeddings_available():
                return
            logger.info("Embedding transcript segments for search...")
            self._emit("pipeline.stage", meeting_id=meeting_id, stage="embedding")
            embedder = Embedder()
            texts = [seg.text.strip() for seg in transcript.segments if seg.text.strip()]
            if not texts:
                return
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
            if meeting_id and self._db_available():
                result = self._db.try_call(
                    self._db.repo.store_embeddings(meeting_id, emb_records),
                    timeout=30.0,
                    what="embedding storage",
                )
                if result is not None or emb_records == []:
                    logger.info("Stored %d segment embeddings", len(emb_records))
        except Exception as e:
            logger.warning("Embedding failed (search will still work without it): %s", e)

    def _write_outputs(
        self,
        summary,
        transcript,
        started_at: float,
        duration_seconds: float,
        meeting_id: str | None,
        notion_page_id: str | None,
    ) -> None:
        self._emit("pipeline.stage", meeting_id=meeting_id, stage="writing")

        md_path: str | None = None
        for source, writer in (
            ("markdown", self._md_writer),
            ("notion", self._notion_writer),
        ):
            if writer is None:
                continue
            try:
                result = writer.write(summary, transcript, started_at, duration_seconds)
                logger.info("%s output: %s", source.capitalize(), result)
                if source == "markdown" and result is not None:
                    md_path = str(result)
            except Exception as e:
                logger.error("%s write failed: %s", source.capitalize(), e, exc_info=True)
            if writer.last_error:
                self._emit(
                    "pipeline.warning",
                    meeting_id=meeting_id,
                    source=source,
                    message=str(writer.last_error),
                )

        if md_path and meeting_id:
            self._update(meeting_id, markdown_path=md_path)

        # Reprocess: archive the previously written Notion page only once
        # its replacement exists — a failed write must not leave the
        # meeting with no page at all.
        new_page_id = getattr(self._notion_writer, "last_page_id", None)
        if notion_page_id and new_page_id and notion_page_id != new_page_id:
            try:
                self._notion_writer.archive_page(notion_page_id)
            except Exception as e:
                logger.warning("Could not archive previous Notion page: %s", e)
        if new_page_id and meeting_id:
            self._update(meeting_id, notion_page_id=new_page_id)

    def _dispatch_post_processing(
        self, meeting_id: str | None, transcript, started_at: float, is_reprocess: bool
    ) -> None:
        if not meeting_id or not self._db_available():
            return
        self._db.schedule(
            self._post_process_async(meeting_id, transcript, started_at, is_reprocess),
            what="post-processing",
        )

    async def _post_process_async(
        self, meeting_id: str, transcript, started_at: float, is_reprocess: bool
    ) -> None:
        """Async post-processing: action items, analytics. Non-fatal."""
        try:
            if self._config.action_items.auto_extract:
                await self._extract_action_items(meeting_id, transcript, is_reprocess)
        except Exception:
            logger.warning("Action item extraction failed", exc_info=True)
        try:
            voice_cfg = getattr(self._config, "voice_id", None)
            if voice_cfg and voice_cfg.suggest_from_transcript:
                await self._suggest_speaker_names(meeting_id, transcript)
        except Exception:
            logger.warning("Speaker-name suggestion failed", exc_info=True)
        try:
            tagging_cfg = getattr(self._config, "tagging", None)
            if tagging_cfg and tagging_cfg.enabled and tagging_cfg.auto_assign:
                await self._auto_assign_client_project(meeting_id)
        except Exception:
            logger.warning("Client/project auto-assignment failed", exc_info=True)
        try:
            await self._scan_trackers(meeting_id, transcript)
        except Exception:
            logger.warning("Tracker scan failed", exc_info=True)
        try:
            insights_cfg = getattr(self._config, "insights", None)
            if insights_cfg and insights_cfg.enabled and insights_cfg.auto_extract:
                await self._extract_insights(meeting_id, transcript)
        except Exception:
            logger.warning("Insight extraction failed", exc_info=True)
        try:
            autos_cfg = getattr(self._config, "automations", None)
            if autos_cfg and autos_cfg.enabled:
                await self._run_automations(meeting_id)
        except Exception:
            logger.warning("Automations run failed", exc_info=True)
        try:
            await self._refresh_analytics(started_at)
        except Exception:
            logger.warning("Analytics refresh failed", exc_info=True)

    async def _extract_action_items(self, meeting_id: str, transcript, is_reprocess: bool) -> None:
        from src.action_items.extractor import ActionItemExtractor
        from src.action_items.repository import ActionItemRepository

        if self._db.database is None:
            return
        extractor = ActionItemExtractor(
            summarisation_config=self._config.summarisation,
            config=self._config.action_items,
        )
        # The LLM call is blocking HTTP — keep it off the API event loop.
        items = await asyncio.to_thread(extractor.extract, transcript)
        ai_repo = ActionItemRepository(self._db.database)
        if is_reprocess:
            # A re-run replaces what extraction previously produced;
            # manually created items are never touched.
            await ai_repo.delete_extracted_for_meeting(meeting_id)
        if not items:
            return
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

    async def _suggest_speaker_names(self, meeting_id: str, transcript) -> None:
        """Store transcript-evidence name suggestions as candidate mappings."""
        from src.people.suggester import SpeakerSuggester

        suggester = SpeakerSuggester(self._config.summarisation)
        remote_label = self._config.diarisation.remote_label
        # The LLM call is blocking HTTP — keep it off the API event loop.
        suggestions = await asyncio.to_thread(suggester.suggest, transcript, remote_label)
        for suggestion in suggestions:
            name = suggestion["suggested_name"]
            await self._db.repo.set_speaker_name(
                meeting_id,
                f"candidate:{name}",
                name,
                source="transcript",
            )
        if suggestions:
            logger.info(
                "Stored %d speaker-name suggestion(s) from transcript evidence",
                len(suggestions),
            )
            self._emit(
                "speakers.suggested",
                meeting_id=meeting_id,
                suggestions=suggestions,
            )

    async def _auto_assign_client_project(self, meeting_id: str) -> None:
        """LLM assignment for meetings the deterministic pass left blank."""
        from src.tagging.assigner import LlmAssigner
        from src.tagging.repository import ClientProjectRepository

        if self._db.database is None:
            return
        meeting = await self._db.repo.get_meeting(meeting_id)
        if meeting is None or meeting.client_id or meeting.project_id:
            return
        cp_repo = ClientProjectRepository(self._db.database)
        roster = await cp_repo.roster()
        if not roster["clients"] and not roster["projects"]:
            return
        try:
            attendees = json.loads(meeting.attendees_json or "[]")
        except (ValueError, TypeError):
            attendees = []
        assigner = LlmAssigner(self._config.summarisation, self._config.tagging)
        # The LLM call is blocking HTTP — keep it off the API event loop.
        assignment = await asyncio.to_thread(
            assigner.assign,
            roster,
            title=meeting.title,
            summary_markdown=meeting.summary_markdown or "",
            attendees=attendees,
        )
        if assignment is None:
            return
        await self._db.repo.update_meeting(
            meeting_id,
            client_id=assignment.client_id,
            project_id=assignment.project_id,
            assignment_source="auto",
            assignment_confidence=assignment.confidence,
        )
        logger.info(
            "LLM assigned meeting %s to client=%s project=%s (%.2f)",
            meeting_id,
            assignment.client_id,
            assignment.project_id,
            assignment.confidence,
        )
        self._emit(
            "meeting.assigned",
            meeting_id=meeting_id,
            client_id=assignment.client_id,
            project_id=assignment.project_id,
            confidence=assignment.confidence,
        )

    async def _extract_insights(self, meeting_id: str, transcript) -> None:
        from src.insights.extractor import InsightExtractor
        from src.insights.repository import InsightRepository

        if self._db.database is None:
            return
        repo = InsightRepository(self._db.database)
        definitions = await repo.list_definitions(enabled_only=True)
        results = []
        if definitions:
            extractor = InsightExtractor(self._config.summarisation)
            # Blocking HTTP — keep it off the API event loop.
            results = await asyncio.to_thread(extractor.extract, transcript, definitions)
        # Always replace — a reprocess (or a since-disabled definition) must
        # clear stale rows even when nothing new is extracted.
        await repo.replace_results_for_meeting(meeting_id, results)
        if results:
            self._emit("insights.extracted", meeting_id=meeting_id, count=len(results))

    async def _run_automations(self, meeting_id: str) -> None:
        from src.automations.evaluator import build_meeting_context, matches
        from src.automations.executor import ActionExecutor
        from src.automations.repository import AutomationRepository

        if self._db.database is None:
            return
        auto_repo = AutomationRepository(self._db.database)
        rules = await auto_repo.list_rules(enabled_only=True)
        if not rules:
            return
        meeting = await self._db.repo.get_meeting(meeting_id)
        if meeting is None:
            return
        # One snapshot for the whole run — match all rules before executing so
        # an apply_tag cannot cascade into another rule's match.
        context = build_meeting_context(meeting)
        matched = [r for r in rules if matches(context, r)]
        if not matched:
            return
        executor = ActionExecutor(self._db.repo, self._emit)
        for rule in matched:
            already = await auto_repo.has_dispatched(rule["id"], meeting_id)
            await executor.run_rule(rule, context, meeting_id, run_side_effects=not already)
            await auto_repo.record_dispatch(rule["id"], meeting_id)
        self._emit(
            "automations.fired",
            meeting_id=meeting_id,
            rules=[{"id": r["id"], "name": r["name"]} for r in matched],
        )

    async def _scan_trackers(self, meeting_id: str, transcript) -> None:
        """Match enabled keyword trackers against the fresh transcript."""
        from src.trackers.repository import TrackerRepository
        from src.trackers.scanner import scan_transcript

        if self._db.database is None:
            return
        tracker_repo = TrackerRepository(self._db.database)
        trackers = await tracker_repo.list_trackers(enabled_only=True)
        hits = scan_transcript(transcript, trackers) if trackers else []
        # Always replace — a reprocess with trackers since disabled or
        # edited must clear the stale hits from the previous scan.
        await tracker_repo.replace_hits_for_meeting(meeting_id, hits)
        if hits:
            by_tracker: dict[str, int] = {}
            for hit in hits:
                by_tracker[hit["tracker_id"]] = by_tracker.get(hit["tracker_id"], 0) + 1
            names = {t["id"]: t["name"] for t in trackers}
            summary = [
                {"tracker_id": tid, "name": names.get(tid, ""), "count": count}
                for tid, count in by_tracker.items()
            ]
            logger.info("Tracker hits in meeting %s: %s", meeting_id, summary)
            self._emit("tracker.hits", meeting_id=meeting_id, trackers=summary)

    async def _refresh_analytics(self, started_at: float) -> None:
        from src.action_items.repository import ActionItemRepository
        from src.analytics.engine import AnalyticsEngine
        from src.analytics.repository import AnalyticsRepository

        if self._db.database is None:
            return
        analytics_repo = AnalyticsRepository(self._db.database)
        ai_repo = ActionItemRepository(self._db.database)
        engine = AnalyticsEngine(
            config=self._config.analytics,
            meeting_repo=self._db.repo,
            analytics_repo=analytics_repo,
            action_item_repo=ai_repo,
        )
        # Refresh the period the meeting belongs to — for a live run
        # that is today; for a reprocess it may be an older day.
        day = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d")
        await engine.refresh_period("daily", day)


def derive_source_paths(audio_path: Path, temp_audio_dir: str | Path) -> dict[str, Path | None]:
    """Locate the per-source WAVs that a capture session left behind.

    Capture writes ``meeting_<ts>_system.wav`` / ``meeting_<ts>_mic.wav``
    next to the merged ``meeting_<ts>.wav`` in the temp dir; the merged
    file is later hard-linked into the durable audio dir under the same
    name. Reprocess uses this to recover the mic source for the energy
    diariser while it still survives the temp-retention sweep.
    """
    stem = audio_path.stem
    base = Path(temp_audio_dir).expanduser()
    mic = base / f"{stem}_mic.wav"
    system = base / f"{stem}_system.wav"
    return {
        "mic": mic if mic.exists() else None,
        "system": system if system.exists() else None,
    }
