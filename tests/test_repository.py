"""Tests for MeetingRepository CRUD operations."""

import time

import pytest

from src.db.repository import MeetingRepository


@pytest.mark.asyncio
async def test_create_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    assert meeting_id
    assert len(meeting_id) == 36  # UUID format


@pytest.mark.asyncio
async def test_get_meeting(repo: MeetingRepository):
    now = time.time()
    meeting_id = await repo.create_meeting(started_at=now)
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.id == meeting_id
    assert meeting.started_at == now
    assert meeting.status == "recording"


@pytest.mark.asyncio
async def test_get_nonexistent_meeting(repo: MeetingRepository):
    meeting = await repo.get_meeting("nonexistent-id")
    assert meeting is None


@pytest.mark.asyncio
async def test_update_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(meeting_id, title="Test Meeting", status="complete")
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.title == "Test Meeting"
    assert meeting.status == "complete"


@pytest.mark.asyncio
async def test_update_meeting_invalid_field(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    with pytest.raises(ValueError, match="Cannot update"):
        await repo.update_meeting(meeting_id, id="hacked")


@pytest.mark.asyncio
async def test_update_meeting_tags(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(meeting_id, tags=["standup", "team"])
    meeting = await repo.get_meeting(meeting_id)
    assert meeting.tags == ["standup", "team"]


@pytest.mark.asyncio
async def test_list_meetings_pagination(repo: MeetingRepository):
    now = time.time()
    for i in range(5):
        await repo.create_meeting(started_at=now + i)

    page1 = await repo.list_meetings(limit=3, offset=0)
    page2 = await repo.list_meetings(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 2
    # Newest first
    assert page1[0].started_at > page1[2].started_at


@pytest.mark.asyncio
async def test_list_meetings_status_filter(repo: MeetingRepository):
    now = time.time()
    id1 = await repo.create_meeting(started_at=now, status="recording")
    id2 = await repo.create_meeting(started_at=now + 1, status="complete")

    recording = await repo.list_meetings(status="recording")
    complete = await repo.list_meetings(status="complete")
    assert len(recording) == 1
    assert recording[0].id == id1
    assert len(complete) == 1
    assert complete[0].id == id2


@pytest.mark.asyncio
async def test_delete_meeting(repo: MeetingRepository):
    meeting_id = await repo.create_meeting(started_at=time.time())
    deleted = await repo.delete_meeting(meeting_id)
    assert deleted is True
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is None


@pytest.mark.asyncio
async def test_delete_nonexistent_meeting(repo: MeetingRepository):
    deleted = await repo.delete_meeting("nonexistent")
    assert deleted is False


@pytest.mark.asyncio
async def test_count_meetings(repo: MeetingRepository):
    assert await repo.count_meetings() == 0
    await repo.create_meeting(started_at=time.time(), status="complete")
    await repo.create_meeting(started_at=time.time(), status="recording")
    assert await repo.count_meetings() == 2
    assert await repo.count_meetings(status="complete") == 1


@pytest.mark.asyncio
async def test_search_meetings_by_title(repo: MeetingRepository):
    """Search finds meetings by title via FTS or LIKE fallback."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(
        mid,
        title="Sprint Planning Review",
        status="complete",
        summary_markdown="Discussed the sprint backlog items.",
    )
    # FTS content-sync may not work in test since transcript_text isn't a
    # real column in meetings. Manually populate FTS index.
    try:
        await repo._db.conn.execute(
            "INSERT INTO meetings_fts (rowid, title, summary_markdown, transcript_text) "
            "SELECT rowid, title, summary_markdown, '' FROM meetings WHERE id = ?",
            (mid,),
        )
        await repo._db.conn.commit()
    except Exception:
        pass  # FTS may not be available

    results = await repo.search_meetings("Sprint")
    assert len(results) >= 1
    assert results[0].title == "Sprint Planning Review"


@pytest.mark.asyncio
async def test_search_meetings_no_results(repo: MeetingRepository):
    await repo.create_meeting(started_at=time.time())
    results = await repo.search_meetings("nonexistent query xyz")
    assert len(results) == 0


@pytest.mark.asyncio
async def test_cleanup_old_meetings(repo: MeetingRepository, tmp_path):
    # Create an old meeting (91 days ago)
    old_time = time.time() - (91 * 86400)
    old_id = await repo.create_meeting(started_at=old_time)

    # Create a recent meeting
    await repo.create_meeting(started_at=time.time())

    result = await repo.cleanup_old_meetings(
        audio_retention_days=0,
        record_retention_days=90,
    )
    assert result["records_deleted"] == 1

    # Old meeting gone, recent one still exists
    assert await repo.get_meeting(old_id) is None
    assert await repo.count_meetings() == 1


@pytest.mark.asyncio
async def test_cleanup_audio_only_retention(repo: MeetingRepository, tmp_path):
    """Audio-only retention nullifies audio_path but keeps the record."""
    # Create an old meeting (10 days ago) with an audio file.
    old_time = time.time() - (10 * 86400)
    old_id = await repo.create_meeting(started_at=old_time)
    audio_file = tmp_path / "meeting.wav"
    audio_file.write_bytes(b"\x00" * 100)
    await repo.update_meeting(old_id, audio_path=str(audio_file))

    result = await repo.cleanup_old_meetings(
        audio_retention_days=1,
        record_retention_days=0,  # 0 = keep records forever
    )
    assert result["audio_deleted"] == 1

    # Record should still exist but audio_path should be NULL.
    meeting = await repo.get_meeting(old_id)
    assert meeting is not None
    assert meeting.audio_path is None


@pytest.mark.asyncio
async def test_update_meeting_empty_tags_list(repo: MeetingRepository):
    """Updating tags to an empty list should round-trip via JSON correctly."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, tags=["initial"])
    await repo.update_meeting(mid, tags=[])
    meeting = await repo.get_meeting(mid)
    assert meeting.tags == []


@pytest.mark.asyncio
async def test_search_meetings_empty_query(repo: MeetingRepository):
    """An empty search query should not crash."""
    await repo.create_meeting(started_at=time.time())
    # Should not raise — either returns results or empty list.
    results = await repo.search_meetings("")
    assert isinstance(results, list)


# ------------------------------------------------------------------
# Embedding storage / retrieval tests
# ------------------------------------------------------------------


def _make_embeddings(texts: list[str]) -> list[dict]:
    """Helper: build embedding dicts with synthetic vectors."""
    return [
        {
            "segment_index": i,
            "embedding": [float(i) * 0.1, float(i) * 0.2, float(i) * 0.3],
            "text": text,
            "speaker": "Me" if i % 2 == 0 else "Remote",
            "start_time": 1000.0 + i * 5.0,
        }
        for i, text in enumerate(texts)
    ]


@pytest.mark.asyncio
async def test_store_and_retrieve_embeddings(repo: MeetingRepository):
    """Store embeddings then retrieve them and verify data matches."""
    meeting_id = await repo.create_meeting(started_at=time.time())
    embeddings = _make_embeddings(["Hello everyone.", "Let's begin."])
    await repo.store_embeddings(meeting_id, embeddings)

    result = await repo.get_meeting_embeddings(meeting_id)
    assert len(result) == 2
    assert result[0]["text"] == "Hello everyone."
    assert result[0]["segment_index"] == 0
    assert result[0]["speaker"] == "Me"
    assert result[0]["start_time"] == 1000.0
    assert result[0]["embedding"] == pytest.approx(embeddings[0]["embedding"])
    assert result[1]["text"] == "Let's begin."
    assert result[1]["embedding"] == pytest.approx(embeddings[1]["embedding"])


@pytest.mark.asyncio
async def test_store_embeddings_replaces_existing(repo: MeetingRepository):
    """Storing embeddings twice for the same meeting replaces the first batch."""
    meeting_id = await repo.create_meeting(started_at=time.time())

    first_batch = _make_embeddings(["First segment."])
    await repo.store_embeddings(meeting_id, first_batch)

    second_batch = _make_embeddings(["Replaced segment.", "New second segment."])
    await repo.store_embeddings(meeting_id, second_batch)

    result = await repo.get_meeting_embeddings(meeting_id)
    assert len(result) == 2
    assert result[0]["text"] == "Replaced segment."
    assert result[1]["text"] == "New second segment."


@pytest.mark.asyncio
async def test_get_meeting_embeddings_filters_by_meeting(repo: MeetingRepository):
    """get_meeting_embeddings only returns embeddings for the requested meeting."""
    mid_a = await repo.create_meeting(started_at=time.time())
    mid_b = await repo.create_meeting(started_at=time.time())

    await repo.store_embeddings(mid_a, _make_embeddings(["Meeting A segment."]))
    await repo.store_embeddings(mid_b, _make_embeddings(["Meeting B segment."]))

    result_a = await repo.get_meeting_embeddings(mid_a)
    result_b = await repo.get_meeting_embeddings(mid_b)

    assert len(result_a) == 1
    assert result_a[0]["text"] == "Meeting A segment."
    assert len(result_b) == 1
    assert result_b[0]["text"] == "Meeting B segment."


@pytest.mark.asyncio
async def test_get_all_embeddings(repo: MeetingRepository):
    """get_all_embeddings returns embeddings from all meetings."""
    mid_a = await repo.create_meeting(started_at=time.time())
    mid_b = await repo.create_meeting(started_at=time.time())

    await repo.store_embeddings(mid_a, _make_embeddings(["Segment A."]))
    await repo.store_embeddings(mid_b, _make_embeddings(["Segment B1.", "Segment B2."]))

    result = await repo.get_all_embeddings()
    assert len(result) == 3
    texts = {r["text"] for r in result}
    assert texts == {"Segment A.", "Segment B1.", "Segment B2."}


# ------------------------------------------------------------------
# Speaker name mapping tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_speaker_names(repo: MeetingRepository):
    """Set a speaker name then retrieve it."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.set_speaker_name(mid, "SPEAKER_00", "Alice")

    names = await repo.get_speaker_names(mid)
    assert len(names) == 1
    assert names[0]["speaker_id"] == "SPEAKER_00"
    assert names[0]["display_name"] == "Alice"
    assert names[0]["source"] == "manual"
    assert isinstance(names[0]["created_at"], float)


@pytest.mark.asyncio
async def test_set_speaker_name_upsert(repo: MeetingRepository):
    """Setting the same speaker_id twice updates the display_name."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.set_speaker_name(mid, "SPEAKER_00", "Alice")
    await repo.set_speaker_name(mid, "SPEAKER_00", "Alicia")

    names = await repo.get_speaker_names(mid)
    assert len(names) == 1
    assert names[0]["display_name"] == "Alicia"


@pytest.mark.asyncio
async def test_get_global_speaker_names(repo: MeetingRepository):
    """Global speaker names returns unique speakers across meetings."""
    mid1 = await repo.create_meeting(started_at=time.time())
    mid2 = await repo.create_meeting(started_at=time.time())
    await repo.set_speaker_name(mid1, "SPEAKER_00", "Alice")
    await repo.set_speaker_name(mid2, "SPEAKER_01", "Bob")

    global_names = await repo.get_global_speaker_names()
    assert len(global_names) == 2
    display_names = {n["display_name"] for n in global_names}
    assert display_names == {"Alice", "Bob"}


@pytest.mark.asyncio
async def test_speaker_name_updates_transcript(repo: MeetingRepository):
    """Setting a speaker name updates speaker labels in transcript_json."""
    import json

    mid = await repo.create_meeting(started_at=time.time())
    transcript_data = {
        "segments": [
            {"start": 0, "end": 5, "text": "Hello.", "speaker": "SPEAKER_00"},
            {"start": 5, "end": 10, "text": "Hi there.", "speaker": "SPEAKER_01"},
        ],
    }
    await repo.update_meeting(mid, transcript_json=json.dumps(transcript_data))

    await repo.set_speaker_name(mid, "SPEAKER_00", "Alice")

    meeting = await repo.get_meeting(mid)
    updated = json.loads(meeting.transcript_json)
    assert updated["segments"][0]["speaker"] == "Alice"
    assert updated["segments"][1]["speaker"] == "SPEAKER_01"


# ------------------------------------------------------------------
# Stale-status recovery (Bug C2)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_stale_inflight_meetings_flips_transcribing_to_error(
    repo: MeetingRepository,
):
    """Meetings stuck in 'transcribing' on startup must be flipped to 'error'.

    Reproduces Bug C2: when the daemon crashes mid-pipeline, the meeting row
    stays in 'transcribing' forever because no code resets it. The UI doesn't
    expose a Retry button for 'transcribing' rows, so the user is wedged
    until restart — and even after restart, nothing fixes the row.
    """
    mid = await repo.create_meeting(started_at=time.time(), status="transcribing")

    flipped = await repo.reset_stale_inflight_meetings()

    assert flipped == 1
    meeting = await repo.get_meeting(mid)
    assert meeting.status == "error"


@pytest.mark.asyncio
async def test_reset_stale_inflight_meetings_flips_recording_to_error(
    repo: MeetingRepository,
):
    """Same as above but for the 'recording' transient status."""
    mid = await repo.create_meeting(started_at=time.time(), status="recording")

    flipped = await repo.reset_stale_inflight_meetings()

    assert flipped == 1
    meeting = await repo.get_meeting(mid)
    assert meeting.status == "error"


@pytest.mark.asyncio
async def test_reset_stale_inflight_meetings_leaves_terminal_statuses_alone(
    repo: MeetingRepository,
):
    """Reset must not touch complete/error/pending rows."""
    now = time.time()
    complete_id = await repo.create_meeting(started_at=now, status="complete")
    error_id = await repo.create_meeting(started_at=now, status="error")
    pending_id = await repo.create_meeting(started_at=now, status="pending")

    flipped = await repo.reset_stale_inflight_meetings()

    assert flipped == 0
    assert (await repo.get_meeting(complete_id)).status == "complete"
    assert (await repo.get_meeting(error_id)).status == "error"
    assert (await repo.get_meeting(pending_id)).status == "pending"


@pytest.mark.asyncio
async def test_reset_stale_inflight_meetings_handles_mixed_set(
    repo: MeetingRepository,
):
    """A realistic mixed set: only the in-flight rows are flipped."""
    now = time.time()
    transcribing_id = await repo.create_meeting(started_at=now, status="transcribing")
    recording_id = await repo.create_meeting(started_at=now, status="recording")
    complete_id = await repo.create_meeting(started_at=now, status="complete")

    flipped = await repo.reset_stale_inflight_meetings()

    assert flipped == 2
    assert (await repo.get_meeting(transcribing_id)).status == "error"
    assert (await repo.get_meeting(recording_id)).status == "error"
    assert (await repo.get_meeting(complete_id)).status == "complete"


# ------------------------------------------------------------------
# Batched fetch
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_meetings_by_ids_preserves_order(repo: MeetingRepository):
    """The result list preserves the order of the requested ids."""
    now = time.time()
    ids = [await repo.create_meeting(started_at=now + i) for i in range(3)]
    out = await repo.get_meetings_by_ids(list(reversed(ids)))
    assert [m.id for m in out] == list(reversed(ids))


@pytest.mark.asyncio
async def test_get_meetings_by_ids_single(repo: MeetingRepository):
    """A single-id batch fetch matches get_meeting()."""
    mid = await repo.create_meeting(started_at=time.time())
    await repo.update_meeting(mid, title="Batched")
    [m] = await repo.get_meetings_by_ids([mid])
    assert m.title == "Batched"
