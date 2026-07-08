"""Tests for keyword trackers — scanner + repository."""

import pytest

from src.trackers.repository import TrackerRepository
from src.trackers.scanner import scan_transcript
from src.transcriber import Transcript, TranscriptSegment


def _transcript(texts):
    return Transcript(
        segments=[
            TranscriptSegment(start=float(i * 5), end=float(i * 5 + 5), text=t)
            for i, t in enumerate(texts)
        ],
        language="en",
        language_probability=0.99,
        duration_seconds=float(len(texts) * 5),
    )


@pytest.fixture
async def tracker_repo(db):
    return TrackerRepository(db)


# ----------------------------------------------------------------------
# Scanner
# ----------------------------------------------------------------------


def test_scan_matches_word_boundary_case_insensitive():
    trackers = [{"id": "t1", "enabled": True, "keywords": ["pricing", "Acme"]}]
    transcript = _transcript(
        [
            "Let's discuss PRICING for the new tier",
            "That was surprising to everyone",  # 'pricing' inside a word — no match
            "acme wants a discount",
        ]
    )

    hits = scan_transcript(transcript, trackers)

    assert [(h["segment_index"], h["matched_keyword"]) for h in hits] == [
        (0, "pricing"),
        (2, "Acme"),
    ]
    assert hits[0]["start_time"] == 0.0
    assert "PRICING" in hits[0]["matched_text"]


def test_scan_skips_disabled_trackers_and_short_keywords():
    trackers = [
        {"id": "t1", "enabled": False, "keywords": ["pricing"]},
        {"id": "t2", "enabled": True, "keywords": ["a", " "]},
    ]
    assert scan_transcript(_transcript(["pricing a"]), trackers) == []


def test_scan_one_hit_per_keyword_per_segment():
    trackers = [{"id": "t1", "enabled": True, "keywords": ["budget"]}]
    hits = scan_transcript(_transcript(["budget budget budget"]), trackers)
    assert len(hits) == 1


# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracker_crud(tracker_repo):
    tracker_id = await tracker_repo.create(name="Pricing talk", keywords=["pricing", "cost"])
    tracker = await tracker_repo.get(tracker_id)
    assert tracker["name"] == "Pricing talk"
    assert tracker["keywords"] == ["pricing", "cost"]
    assert tracker["enabled"] is True

    await tracker_repo.update(tracker_id, enabled=False, keywords=["pricing"])
    tracker = await tracker_repo.get(tracker_id)
    assert tracker["enabled"] is False
    assert tracker["keywords"] == ["pricing"]

    assert await tracker_repo.list_trackers(enabled_only=True) == []
    assert len(await tracker_repo.list_trackers()) == 1

    assert await tracker_repo.delete(tracker_id) is True
    assert await tracker_repo.get(tracker_id) is None


@pytest.mark.asyncio
async def test_replace_hits_is_reprocess_safe(tracker_repo, repo):
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    tracker_id = await tracker_repo.create(name="T", keywords=["x"])

    first = [
        {
            "tracker_id": tracker_id,
            "segment_index": 0,
            "matched_keyword": "x",
            "matched_text": "x said",
            "start_time": 0.0,
        },
        {
            "tracker_id": tracker_id,
            "segment_index": 3,
            "matched_keyword": "x",
            "matched_text": "x again",
            "start_time": 15.0,
        },
    ]
    assert await tracker_repo.replace_hits_for_meeting(meeting_id, first) == 2

    second = first[:1]
    assert await tracker_repo.replace_hits_for_meeting(meeting_id, second) == 1
    hits = await tracker_repo.hits_for_meeting(meeting_id)
    assert len(hits) == 1
    assert hits[0]["tracker_name"] == "T"


@pytest.mark.asyncio
async def test_hits_for_tracker_joins_meeting_info(tracker_repo, repo):
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    await repo.update_meeting(meeting_id, title="Budget sync")
    tracker_id = await tracker_repo.create(name="T", keywords=["x"])
    await tracker_repo.replace_hits_for_meeting(
        meeting_id,
        [
            {
                "tracker_id": tracker_id,
                "segment_index": 0,
                "matched_keyword": "x",
                "matched_text": "",
                "start_time": 0.0,
            }
        ],
    )

    hits = await tracker_repo.hits_for_tracker(tracker_id)
    assert hits[0]["meeting_title"] == "Budget sync"
    assert hits[0]["meeting_started_at"] == 1000.0


@pytest.mark.asyncio
async def test_deleting_tracker_cascades_hits(tracker_repo, repo, db):
    meeting_id = await repo.create_meeting(started_at=1000.0, status="complete")
    tracker_id = await tracker_repo.create(name="T", keywords=["x"])
    await tracker_repo.replace_hits_for_meeting(
        meeting_id,
        [
            {
                "tracker_id": tracker_id,
                "segment_index": 0,
                "matched_keyword": "x",
                "matched_text": "",
                "start_time": 0.0,
            }
        ],
    )
    await tracker_repo.delete(tracker_id)
    cursor = await db.conn.execute("SELECT COUNT(*) FROM tracker_hits")
    assert (await cursor.fetchone())[0] == 0
