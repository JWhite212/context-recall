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

    def summarise(self, transcript, template=None):
        self.calls.append((transcript, template))
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
