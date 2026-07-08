"""Tests for src/pipeline_runner.py — the shared post-capture pipeline.

The runner is the single implementation of transcribe → diarise →
summarise → persist → write → post-process used by both the live
orchestrator and the reprocess route. These tests pin its contract:
status transitions, event emissions, speaker handling, output writing,
and the DB bridge's degrade-to-no-op behaviour.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.diariser import EnergyDiariser
from src.pipeline_runner import (
    EMPTY_TRANSCRIPT_ERROR,
    SHORT_TRANSCRIPT_TITLE,
    DbBridge,
    PipelineRunner,
    derive_source_paths,
)
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import AppConfig

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_transcript(texts=("hello world this is a test",), speakers=None):
    segments = []
    for i, text in enumerate(texts):
        seg = TranscriptSegment(start=float(i * 2), end=float(i * 2 + 2), text=text)
        if speakers:
            seg.speaker = speakers[i]
        segments.append(seg)
    return Transcript(
        segments=segments,
        language="en",
        language_probability=0.99,
        duration_seconds=float(len(texts) * 2),
    )


def _make_summary(title="Test Meeting"):
    return MeetingSummary(
        raw_markdown="# Test\n\n## Summary\nA test meeting.",
        title=title,
        tags=["test"],
    )


class FakeTranscriber:
    def __init__(self, transcript=None, error=None):
        self._transcript = transcript if transcript is not None else _make_transcript()
        self._error = error

    def transcribe(self, audio_path, on_segment=None):
        if self._error:
            raise self._error
        if on_segment:
            for seg in self._transcript.segments:
                on_segment(seg)
        return self._transcript


class FakeSummariser:
    def __init__(self, summary=None, error=None):
        self._summary = summary or _make_summary()
        self._error = error
        self.calls = []
        self.contexts = []

    def summarise(self, transcript, template=None, extra_context=None):
        self.calls.append((transcript, template))
        self.contexts.append(extra_context)
        if self._error:
            raise self._error
        return self._summary


class FakeWriter:
    def __init__(self, result="out.md", last_error=None):
        self.result = result
        self.last_error = last_error
        self.calls = []

    def write(self, summary, transcript, started_at, duration_seconds):
        self.calls.append((summary, transcript, started_at, duration_seconds))
        return self.result


class FakeNotionWriter(FakeWriter):
    def __init__(self, page_id="new-page-id", **kwargs):
        super().__init__(**kwargs)
        self.last_page_id = None
        self._page_id = page_id
        self.archived = []

    def write(self, summary, transcript, started_at, duration_seconds):
        self.last_page_id = self._page_id
        return super().write(summary, transcript, started_at, duration_seconds)

    def archive_page(self, page_id):
        self.archived.append(page_id)
        return True


class RecordingDiariser:
    """Stands in for a non-energy (pyannote-style) backend."""

    def __init__(self):
        self.calls = []

    def diarise(self, transcript, audio_path):
        self.calls.append((transcript, audio_path))
        return transcript


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch):
    """Keep the suite hermetic: never load the real sentence-transformer."""
    import src.embeddings as embeddings_mod

    monkeypatch.setattr(embeddings_mod, "is_embeddings_available", lambda: False)


@pytest.fixture
def loop_thread():
    """A real event loop on a background thread, like the API server's."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop

    # Wait for anything fire-and-forgotten onto the loop (post-processing
    # tasks awaiting to_thread) so teardown never destroys a pending task.
    async def _wait_all_tasks():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    try:
        asyncio.run_coroutine_threadsafe(_wait_all_tasks(), loop).result(timeout=6)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def _drain(loop):
    """Wait for everything already scheduled on the loop to finish."""

    async def _noop():
        return None

    asyncio.run_coroutine_threadsafe(_noop(), loop).result(timeout=5)


def _make_repo():
    repo = MagicMock()
    repo.update_meeting = AsyncMock()
    repo.update_fts = AsyncMock()
    repo.set_speaker_name = AsyncMock()
    repo.get_speaker_names = AsyncMock(return_value=[])
    repo.store_embeddings = AsyncMock()
    return repo


def _make_config(tmp_path):
    config = AppConfig()
    config.markdown.enabled = False
    config.markdown.vault_path = str(tmp_path / "vault")
    config.action_items.auto_extract = False
    return config


def _make_runner(config, *, emit=None, db=None, transcriber=None, summariser=None, **kw):
    return PipelineRunner(
        config,
        emit=emit,
        db=db,
        transcriber=transcriber or FakeTranscriber(),
        summariser=summariser or FakeSummariser(),
        **kw,
    )


def _collect_events():
    events = []

    def emit(event_type, **kwargs):
        events.append({"type": event_type, **kwargs})

    return events, emit


# ----------------------------------------------------------------------
# Core status transitions
# ----------------------------------------------------------------------


def test_full_run_persists_complete_and_updates_fts(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    runner = _make_runner(_make_config(tmp_path), emit=emit, db=bridge)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "complete"
    assert result.title == "Test Meeting"
    _drain(loop_thread)
    persist_calls = [c.kwargs for c in repo.update_meeting.call_args_list]
    complete = next(c for c in persist_calls if c.get("status") == "complete")
    assert complete["title"] == "Test Meeting"
    assert complete["word_count"] == 6
    repo.update_fts.assert_awaited_with("m1")
    types = [e["type"] for e in events]
    assert "pipeline.complete" in types
    assert types.index("pipeline.stage") < types.index("pipeline.complete")


def test_empty_transcript_marks_error(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    transcriber = FakeTranscriber(
        transcript=Transcript(
            segments=[], language="en", language_probability=0.0, duration_seconds=0.0
        )
    )
    runner = _make_runner(_make_config(tmp_path), emit=emit, db=bridge, transcriber=transcriber)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "error"
    assert result.error == EMPTY_TRANSCRIPT_ERROR
    _drain(loop_thread)
    repo.update_meeting.assert_awaited_with("m1", status="error")
    errors = [e for e in events if e["type"] == "pipeline.error"]
    assert errors and errors[0]["error"] == EMPTY_TRANSCRIPT_ERROR


def test_transcription_exception_marks_error(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    transcriber = FakeTranscriber(error=RuntimeError("mlx exploded"))
    runner = _make_runner(_make_config(tmp_path), emit=emit, db=bridge, transcriber=transcriber)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "error"
    _drain(loop_thread)
    repo.update_meeting.assert_awaited_with("m1", status="error")


def test_short_transcript_completes_without_summarisation(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    summariser = FakeSummariser()
    transcriber = FakeTranscriber(transcript=_make_transcript(texts=("hi bye",)))
    runner = _make_runner(
        _make_config(tmp_path), emit=emit, db=bridge, transcriber=transcriber, summariser=summariser
    )

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "short"
    assert summariser.calls == []
    _drain(loop_thread)
    kwargs = repo.update_meeting.call_args.kwargs
    assert kwargs["title"] == SHORT_TRANSCRIPT_TITLE
    assert kwargs["status"] == "complete"
    repo.update_fts.assert_awaited_with("m1")
    assert [e for e in events if e["type"] == "pipeline.complete"]


def test_summarisation_failure_marks_error(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    runner = _make_runner(
        _make_config(tmp_path),
        emit=emit,
        db=bridge,
        summariser=FakeSummariser(error=RuntimeError("ollama down")),
    )

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "error"
    _drain(loop_thread)
    repo.update_meeting.assert_awaited_with("m1", status="error")
    assert [e for e in events if e["type"] == "pipeline.error" and e["stage"] == "summarising"]


def test_transcript_segment_events_forwarded(tmp_path):
    events, emit = _collect_events()
    runner = _make_runner(_make_config(tmp_path), emit=emit)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    seg_events = [e for e in events if e["type"] == "transcript.segment"]
    assert len(seg_events) == 1
    assert seg_events[0]["segment"]["text"] == "hello world this is a test"


# ----------------------------------------------------------------------
# Diarisation dispatch
# ----------------------------------------------------------------------


def test_energy_diariser_failure_emits_warning_and_continues(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    config = _make_config(tmp_path)
    diariser = EnergyDiariser(config.diarisation)  # raises without mic path
    runner = _make_runner(config, emit=emit, db=bridge, diariser=diariser)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0, mic_audio_path=None)

    assert result.status == "complete"
    warnings = [e for e in events if e["type"] == "pipeline.warning"]
    assert warnings and warnings[0]["source"] == "diarisation"


def test_non_energy_diariser_called_without_mic_kwarg(tmp_path):
    diariser = RecordingDiariser()
    runner = _make_runner(_make_config(tmp_path), diariser=diariser)

    result = runner.run(
        tmp_path / "a.wav", "m1", started_at=1000.0, mic_audio_path=tmp_path / "mic.wav"
    )

    assert result.status == "complete"
    assert len(diariser.calls) == 1  # would TypeError if mic kwarg were passed


# ----------------------------------------------------------------------
# Speaker mappings and attendee enrichment
# ----------------------------------------------------------------------


def test_preserve_mappings_reapplies_stored_renames(tmp_path, loop_thread):
    repo = _make_repo()
    repo.get_speaker_names = AsyncMock(
        return_value=[
            {"speaker_id": "Remote", "display_name": "Sarah", "source": "manual"},
            {"speaker_id": "candidate:Bob", "display_name": "Bob", "source": "calendar"},
        ]
    )
    bridge = DbBridge(repo, loop_thread)
    transcript = _make_transcript(
        texts=("hello there team", "hi and welcome everyone"), speakers=["Me", "Remote"]
    )
    runner = _make_runner(
        _make_config(tmp_path), db=bridge, transcriber=FakeTranscriber(transcript=transcript)
    )

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0, preserve_mappings=True)

    assert transcript.segments[1].speaker == "Sarah"
    assert transcript.segments[0].speaker == "Me"


def test_attendee_enrichment_renames_remote_in_two_speaker_meeting(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    transcript = _make_transcript(
        texts=("hello there team", "hi and welcome everyone"), speakers=["Me", "Remote"]
    )
    runner = _make_runner(
        _make_config(tmp_path), db=bridge, transcriber=FakeTranscriber(transcript=transcript)
    )

    runner.run(
        tmp_path / "a.wav",
        "m1",
        started_at=1000.0,
        attendees=[{"name": "Sarah Chen", "email": "s@x.com"}],
    )

    assert transcript.segments[1].speaker == "Sarah Chen"
    _drain(loop_thread)
    repo.set_speaker_name.assert_awaited_with(
        "m1", "candidate:Sarah Chen", "Sarah Chen", source="calendar"
    )


def test_stored_manual_rename_wins_over_attendee_auto_rename(tmp_path, loop_thread):
    """A reprocess must respect the user's manual label even when the
    calendar attendee auto-rename would suggest a different name."""
    repo = _make_repo()
    repo.get_speaker_names = AsyncMock(
        return_value=[{"speaker_id": "Remote", "display_name": "Sarah", "source": "manual"}]
    )
    bridge = DbBridge(repo, loop_thread)
    transcript = _make_transcript(
        texts=("hello there team", "hi and welcome everyone"), speakers=["Me", "Remote"]
    )
    runner = _make_runner(
        _make_config(tmp_path), db=bridge, transcriber=FakeTranscriber(transcript=transcript)
    )

    runner.run(
        tmp_path / "a.wav",
        "m1",
        started_at=1000.0,
        attendees=[{"name": "Different Name", "email": "d@x.com"}],
        preserve_mappings=True,
    )

    # Mapping applied first; "Remote" no longer exists for the auto-rename.
    assert transcript.segments[1].speaker == "Sarah"


# ----------------------------------------------------------------------
# Output writers
# ----------------------------------------------------------------------


def test_writers_invoked_and_warning_emitted_on_writer_error(tmp_path):
    events, emit = _collect_events()
    md = FakeWriter(last_error="disk full")
    runner = _make_runner(_make_config(tmp_path), emit=emit, md_writer=md)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "complete"
    assert len(md.calls) == 1
    warnings = [e for e in events if e["type"] == "pipeline.warning"]
    assert warnings and warnings[0]["source"] == "markdown"
    assert "disk full" in warnings[0]["message"]


def test_notion_reprocess_archives_old_page_and_persists_new_id(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    notion = FakeNotionWriter(page_id="new-page")
    runner = _make_runner(_make_config(tmp_path), db=bridge, notion_writer=notion)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0, notion_page_id="old-page")

    assert notion.archived == ["old-page"]
    _drain(loop_thread)
    page_updates = [
        c.kwargs for c in repo.update_meeting.call_args_list if "notion_page_id" in c.kwargs
    ]
    assert page_updates and page_updates[0]["notion_page_id"] == "new-page"


def test_no_db_bridge_still_writes_outputs(tmp_path):
    md = FakeWriter()
    runner = _make_runner(_make_config(tmp_path), md_writer=md)

    result = runner.run(tmp_path / "a.wav", None, started_at=1000.0)

    assert result.status == "complete"
    assert len(md.calls) == 1


def test_embeddings_stored_when_available(tmp_path, loop_thread, monkeypatch):
    import src.embeddings as embeddings_mod

    monkeypatch.setattr(embeddings_mod, "is_embeddings_available", lambda: True)
    fake_embedder = MagicMock()
    fake_embedder.embed.return_value = [[0.1] * 4]
    monkeypatch.setattr(embeddings_mod, "Embedder", lambda: fake_embedder)

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    events, emit = _collect_events()
    runner = _make_runner(_make_config(tmp_path), emit=emit, db=bridge)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    _drain(loop_thread)
    repo.store_embeddings.assert_awaited_once()
    meeting_id, records = repo.store_embeddings.await_args.args
    assert meeting_id == "m1"
    assert records[0]["text"] == "hello world this is a test"
    assert "embedding" in [e["type"] for e in events if e["type"] == "pipeline.stage"] or [
        e for e in events if e.get("stage") == "embedding"
    ]


# ----------------------------------------------------------------------
# Post-processing
# ----------------------------------------------------------------------


def test_post_processing_replaces_extracted_items_on_reprocess(tmp_path, loop_thread):
    repo = _make_repo()
    database = MagicMock()
    bridge = DbBridge(repo, loop_thread, database=database)
    config = _make_config(tmp_path)
    config.action_items.auto_extract = True
    runner = _make_runner(config, db=bridge)

    ai_repo = MagicMock()
    ai_repo.delete_extracted_for_meeting = AsyncMock()
    ai_repo.create = AsyncMock()

    with (
        patch("src.action_items.extractor.ActionItemExtractor") as extractor_cls,
        patch("src.action_items.repository.ActionItemRepository", return_value=ai_repo),
        patch("src.analytics.engine.AnalyticsEngine") as engine_cls,
    ):
        extractor_cls.return_value.extract.return_value = [{"title": "Do the thing"}]
        engine_cls.return_value.refresh_period = AsyncMock()
        asyncio.run(
            runner._post_process_async(
                "m1", _make_transcript(), started_at=1000.0, is_reprocess=True
            )
        )

    ai_repo.delete_extracted_for_meeting.assert_awaited_once_with("m1")
    ai_repo.create.assert_awaited_once()
    assert ai_repo.create.call_args.kwargs["source"] == "extracted"


def test_post_processing_appends_without_delete_on_live_run(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    config.action_items.auto_extract = True
    runner = _make_runner(config, db=bridge)

    ai_repo = MagicMock()
    ai_repo.delete_extracted_for_meeting = AsyncMock()
    ai_repo.create = AsyncMock()

    with (
        patch("src.action_items.extractor.ActionItemExtractor") as extractor_cls,
        patch("src.action_items.repository.ActionItemRepository", return_value=ai_repo),
        patch("src.analytics.engine.AnalyticsEngine") as engine_cls,
    ):
        extractor_cls.return_value.extract.return_value = [{"title": "Do the thing"}]
        engine_cls.return_value.refresh_period = AsyncMock()
        asyncio.run(
            runner._post_process_async(
                "m1", _make_transcript(), started_at=1000.0, is_reprocess=False
            )
        )

    ai_repo.delete_extracted_for_meeting.assert_not_awaited()
    ai_repo.create.assert_awaited_once()


def test_analytics_refreshes_period_of_meeting_date(tmp_path, loop_thread):
    """A reprocessed meeting refreshes ITS day's analytics, not today's."""
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    runner = _make_runner(config, db=bridge)

    engine = MagicMock()
    engine.refresh_period = AsyncMock()

    # 2020-06-01 00:00:00 UTC
    old_ts = 1590969600.0
    with (
        patch("src.analytics.engine.AnalyticsEngine", return_value=engine),
        patch("src.analytics.repository.AnalyticsRepository"),
        patch("src.action_items.repository.ActionItemRepository"),
    ):
        asyncio.run(runner._post_process_async("m1", _make_transcript(), old_ts, is_reprocess=True))

    engine.refresh_period.assert_awaited_once_with("daily", "2020-06-01")


# ----------------------------------------------------------------------
# DbBridge degrade behaviour
# ----------------------------------------------------------------------


def test_bridge_unavailable_closes_coroutines_without_warning(tmp_path):
    """A dead bridge must close un-run coroutines (no 'never awaited')."""
    repo = _make_repo()
    bridge = DbBridge(repo, loop=None)

    assert not bridge.available
    assert bridge.try_call(repo.update_fts("m1")) is None
    bridge.schedule(repo.update_fts("m2"))
    bridge.update_meeting("m1", status="complete")  # logs, no raise


def test_bridge_with_closed_loop_drops_update_loudly(tmp_path, caplog):
    loop = asyncio.new_event_loop()
    loop.close()
    repo = _make_repo()
    bridge = DbBridge(repo, loop)

    bridge.update_meeting("m1", status="complete")

    assert any("Fields lost" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# from_config and source-path derivation
# ----------------------------------------------------------------------


def test_from_config_builds_writers_per_flags(tmp_path):
    config = _make_config(tmp_path)
    config.markdown.enabled = True
    config.notion.enabled = False
    config.diarisation.enabled = True

    runner = PipelineRunner.from_config(config)

    assert runner._md_writer is not None
    assert runner._notion_writer is None
    assert isinstance(runner._diariser, EnergyDiariser)


def test_derive_source_paths_finds_surviving_mic_wav(tmp_path):
    (tmp_path / "meeting_20260708_101500_mic.wav").write_bytes(b"x" * 100)
    audio = tmp_path / "durable" / "meeting_20260708_101500.wav"

    sources = derive_source_paths(audio, tmp_path)

    assert sources["mic"] == tmp_path / "meeting_20260708_101500_mic.wav"
    assert sources["system"] is None


def test_derive_source_paths_when_nothing_survives(tmp_path):
    audio = tmp_path / "meeting_20260708_101500.wav"

    sources = derive_source_paths(audio, tmp_path)

    assert sources == {"mic": None, "system": None}


# ----------------------------------------------------------------------
# Voice identification stage
# ----------------------------------------------------------------------


def _patch_voice(monkeypatch, profiles, matches):
    """Wire fake voice modules into the runner's lazy imports."""
    import src.people.repository as people_repo_mod
    import src.voice.embedder as embedder_mod
    import src.voice.recognition as recognition_mod

    monkeypatch.setattr(embedder_mod, "is_voice_id_available", lambda: True)
    monkeypatch.setattr(embedder_mod, "VoiceEmbedder", lambda *a, **k: MagicMock())

    class FakePersonRepository:
        def __init__(self, database):
            self.database = database

        async def get_all_voice_profiles(self):
            return profiles

    monkeypatch.setattr(people_repo_mod, "PersonRepository", FakePersonRepository)

    recogniser_calls = []

    class FakeRecogniser:
        def __init__(self, embedder, config):
            self.config = config

        def identify(self, transcript, audio_path, received_profiles):
            recogniser_calls.append({"transcript": transcript, "profiles": received_profiles})
            for match in matches:
                for i in match.segment_indices:
                    transcript.segments[i].speaker = match.new_label
            return matches

    monkeypatch.setattr(recognition_mod, "VoiceRecogniser", FakeRecogniser)
    return recogniser_calls


def test_voice_identification_stores_person_linked_mapping(tmp_path, loop_thread, monkeypatch):
    from src.voice.recognition import VoiceMatch

    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": [1.0, 0.0]}]
    match = VoiceMatch(
        original_label="Remote",
        new_label="Sarah",
        person_id="p1",
        confidence=0.87,
        segment_indices=[1],
    )
    calls = _patch_voice(monkeypatch, profiles, [match])

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    transcript = _make_transcript(
        texts=("hello there team", "hi and welcome everyone"), speakers=["Me", "Remote"]
    )
    runner = _make_runner(
        _make_config(tmp_path), db=bridge, transcriber=FakeTranscriber(transcript=transcript)
    )

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "complete"
    assert len(calls) == 1
    assert calls[0]["profiles"] == profiles
    assert transcript.segments[1].speaker == "Sarah"
    _drain(loop_thread)
    voice_calls = [
        c for c in repo.set_speaker_name.await_args_list if c.kwargs.get("source") == "voice"
    ]
    assert len(voice_calls) == 1
    assert voice_calls[0].args == ("m1", "Remote", "Sarah")
    assert voice_calls[0].kwargs["person_id"] == "p1"
    assert voice_calls[0].kwargs["confidence"] == 0.87


def test_voice_identification_skipped_when_disabled(tmp_path, loop_thread, monkeypatch):
    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": [1.0, 0.0]}]
    calls = _patch_voice(monkeypatch, profiles, [])

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    config.voice_id.enabled = False
    runner = _make_runner(config, db=bridge)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert calls == []


def test_voice_identification_skipped_without_profiles(tmp_path, loop_thread, monkeypatch):
    calls = _patch_voice(monkeypatch, [], [])

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    runner = _make_runner(_make_config(tmp_path), db=bridge)

    result = runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert result.status == "complete"
    assert calls == []


def test_voice_match_beats_attendee_auto_rename(tmp_path, loop_thread, monkeypatch):
    """A voice match consumes the Remote label before attendee enrichment,
    so the calendar's single-attendee heuristic cannot override it."""
    from src.voice.recognition import VoiceMatch

    profiles = [{"person_id": "p1", "name": "Sarah", "embedding": [1.0, 0.0]}]
    match = VoiceMatch(
        original_label="Remote",
        new_label="Sarah",
        person_id="p1",
        confidence=0.9,
        segment_indices=[1],
    )
    _patch_voice(monkeypatch, profiles, [match])

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    transcript = _make_transcript(
        texts=("hello there team", "hi and welcome everyone"), speakers=["Me", "Remote"]
    )
    runner = _make_runner(
        _make_config(tmp_path), db=bridge, transcriber=FakeTranscriber(transcript=transcript)
    )

    runner.run(
        tmp_path / "a.wav",
        "m1",
        started_at=1000.0,
        attendees=[{"name": "Wrong Person", "email": "w@x.com"}],
    )

    assert transcript.segments[1].speaker == "Sarah"


def test_speaker_suggestions_stored_as_candidates(tmp_path, loop_thread, monkeypatch):
    import src.people.suggester as suggester_mod

    class FakeSuggester:
        def __init__(self, config):
            pass

        def suggest(self, transcript, remote_label="Remote"):
            return [
                {
                    "speaker_label": "Remote",
                    "suggested_name": "Marcus",
                    "evidence": "This is Marcus",
                }
            ]

    monkeypatch.setattr(suggester_mod, "SpeakerSuggester", FakeSuggester)

    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    runner = _make_runner(config, db=bridge)

    events, emit = _collect_events()
    runner._emit_cb = emit
    asyncio.run(runner._post_process_async("m1", _make_transcript(), 1000.0, is_reprocess=False))

    candidate_calls = [
        c for c in repo.set_speaker_name.await_args_list if c.kwargs.get("source") == "transcript"
    ]
    assert len(candidate_calls) == 1
    assert candidate_calls[0].args == ("m1", "candidate:Marcus", "Marcus")
    suggested = [e for e in events if e["type"] == "speakers.suggested"]
    assert suggested and suggested[0]["suggestions"][0]["suggested_name"] == "Marcus"


# ----------------------------------------------------------------------
# Client/project assignment
# ----------------------------------------------------------------------

ACME_ROSTER = {
    "clients": [
        {
            "id": "c-acme",
            "name": "Acme Corp",
            "description": "Industrial widgets client.",
            "aliases": [],
            "email_domains": ["acme.com"],
        }
    ],
    "projects": [],
}


class FakeCPRepo:
    def __init__(self, database, roster=None, series_assignment=None):
        self._roster = roster if roster is not None else ACME_ROSTER
        self._series_assignment = series_assignment

    async def roster(self):
        return self._roster

    async def latest_assignment_for_series(self, series_id):
        return self._series_assignment


def _unassigned_meeting(**overrides):
    from types import SimpleNamespace

    defaults = {
        "assignment_source": "",
        "series_id": None,
        "calendar_event_title": "",
        "client_id": None,
        "project_id": None,
        "title": "Weekly sync",
        "summary_markdown": "Portal work discussed.",
        "attendees_json": "[]",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_deterministic_assignment_injects_context_and_persists(tmp_path, loop_thread, monkeypatch):
    import src.tagging.repository as cp_mod

    monkeypatch.setattr(cp_mod, "ClientProjectRepository", FakeCPRepo)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting())
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    summariser = FakeSummariser()
    runner = _make_runner(_make_config(tmp_path), db=bridge, summariser=summariser)

    result = runner.run(
        tmp_path / "a.wav",
        "m1",
        started_at=1000.0,
        attendees=[{"name": "Sarah", "email": "sarah@acme.com"}],
    )

    assert result.status == "complete"
    assert summariser.contexts[0] is not None
    assert "Acme Corp" in summariser.contexts[0]
    assert "Industrial widgets" in summariser.contexts[0]
    _drain(loop_thread)
    assign_calls = [
        c.kwargs
        for c in repo.update_meeting.await_args_list
        if c.kwargs.get("assignment_source") == "auto"
    ]
    assert assign_calls and assign_calls[0]["client_id"] == "c-acme"


def test_manual_assignment_respected_but_context_still_injected(tmp_path, loop_thread, monkeypatch):
    import src.tagging.repository as cp_mod

    monkeypatch.setattr(cp_mod, "ClientProjectRepository", FakeCPRepo)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(
        return_value=_unassigned_meeting(assignment_source="manual", client_id="c-acme")
    )
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    summariser = FakeSummariser()
    runner = _make_runner(_make_config(tmp_path), db=bridge, summariser=summariser)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert summariser.contexts[0] is not None
    assert "Acme Corp" in summariser.contexts[0]
    _drain(loop_thread)
    assign_calls = [
        c.kwargs
        for c in repo.update_meeting.await_args_list
        if c.kwargs.get("assignment_source") == "auto"
    ]
    assert assign_calls == [], "manual assignment must never be overwritten"


def test_no_roster_means_no_context_and_no_assignment(tmp_path, loop_thread, monkeypatch):
    import src.tagging.repository as cp_mod

    class EmptyCPRepo(FakeCPRepo):
        def __init__(self, database):
            super().__init__(database, roster={"clients": [], "projects": []})

    monkeypatch.setattr(cp_mod, "ClientProjectRepository", EmptyCPRepo)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting())
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    summariser = FakeSummariser()
    runner = _make_runner(_make_config(tmp_path), db=bridge, summariser=summariser)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    assert summariser.contexts[0] is None


def test_llm_auto_assignment_runs_in_post_processing(tmp_path, loop_thread, monkeypatch):
    import src.tagging.assigner as assigner_mod
    import src.tagging.repository as cp_mod
    from src.tagging.assigner import Assignment

    monkeypatch.setattr(cp_mod, "ClientProjectRepository", FakeCPRepo)

    class FakeLlmAssigner:
        def __init__(self, summarisation_config, config):
            pass

        def assign(self, roster, *, title, summary_markdown, attendees):
            return Assignment(client_id="c-acme", project_id=None, confidence=0.8, method="llm")

    monkeypatch.setattr(assigner_mod, "LlmAssigner", FakeLlmAssigner)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting())
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    runner = _make_runner(config, db=bridge)

    asyncio.run(runner._post_process_async("m1", _make_transcript(), 1000.0, False))

    assign_calls = [
        c.kwargs
        for c in repo.update_meeting.await_args_list
        if c.kwargs.get("assignment_source") == "auto"
    ]
    assert assign_calls and assign_calls[0]["client_id"] == "c-acme"
    assert assign_calls[0]["assignment_confidence"] == 0.8


def test_llm_auto_assignment_skipped_when_already_assigned(tmp_path, loop_thread, monkeypatch):
    import src.tagging.assigner as assigner_mod
    import src.tagging.repository as cp_mod

    monkeypatch.setattr(cp_mod, "ClientProjectRepository", FakeCPRepo)
    assigner_cls = MagicMock()
    monkeypatch.setattr(assigner_mod, "LlmAssigner", assigner_cls)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting(client_id="c-acme"))
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    runner = _make_runner(_make_config(tmp_path), db=bridge)

    asyncio.run(runner._post_process_async("m1", _make_transcript(), 1000.0, False))

    assigner_cls.assert_not_called()


def test_tracker_scan_runs_in_post_processing(tmp_path, loop_thread, monkeypatch):
    import src.trackers.repository as tracker_repo_mod

    stored = {}

    class FakeTrackerRepo:
        def __init__(self, database):
            pass

        async def list_trackers(self, enabled_only=False):
            return [{"id": "t1", "name": "Pricing", "enabled": True, "keywords": ["hello"]}]

        async def replace_hits_for_meeting(self, meeting_id, hits):
            stored["meeting_id"] = meeting_id
            stored["hits"] = hits
            return len(hits)

    monkeypatch.setattr(tracker_repo_mod, "TrackerRepository", FakeTrackerRepo)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting(client_id="c1"))
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    events, emit = _collect_events()
    runner = _make_runner(_make_config(tmp_path), emit=emit, db=bridge)

    asyncio.run(runner._post_process_async("m1", _make_transcript(), 1000.0, False))

    assert stored["meeting_id"] == "m1"
    assert stored["hits"][0]["matched_keyword"] == "hello"
    hit_events = [e for e in events if e["type"] == "tracker.hits"]
    assert hit_events and hit_events[0]["trackers"][0]["count"] == 1


def test_notion_old_page_kept_when_replacement_write_fails(tmp_path, loop_thread):
    """The previous page must survive a failed replacement write."""
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)

    class FailingNotionWriter(FakeNotionWriter):
        def write(self, summary, transcript, started_at, duration_seconds):
            self.calls.append((summary, transcript, started_at, duration_seconds))
            self.last_page_id = None  # create failed
            return None

    notion = FailingNotionWriter()
    runner = _make_runner(_make_config(tmp_path), db=bridge, notion_writer=notion)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0, notion_page_id="old-page")

    assert notion.archived == []


def test_tracker_scan_clears_hits_when_no_enabled_trackers(tmp_path, loop_thread, monkeypatch):
    import src.trackers.repository as tracker_repo_mod

    replaced = {}

    class EmptyTrackerRepo:
        def __init__(self, database):
            pass

        async def list_trackers(self, enabled_only=False):
            return []

        async def replace_hits_for_meeting(self, meeting_id, hits):
            replaced["meeting_id"] = meeting_id
            replaced["hits"] = hits
            return 0

    monkeypatch.setattr(tracker_repo_mod, "TrackerRepository", EmptyTrackerRepo)

    repo = _make_repo()
    repo.get_meeting = AsyncMock(return_value=_unassigned_meeting(client_id="c1"))
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    runner = _make_runner(_make_config(tmp_path), db=bridge)

    asyncio.run(runner._post_process_async("m1", _make_transcript(), 1000.0, False))

    assert replaced == {"meeting_id": "m1", "hits": []}


def test_short_transcript_clears_stale_summary_fields(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread)
    transcriber = FakeTranscriber(transcript=_make_transcript(texts=("hi bye",)))
    runner = _make_runner(_make_config(tmp_path), db=bridge, transcriber=transcriber)

    runner.run(tmp_path / "a.wav", "m1", started_at=1000.0)

    _drain(loop_thread)
    kwargs = repo.update_meeting.call_args.kwargs
    assert kwargs["summary_markdown"] is None
    assert kwargs["tags"] == []
