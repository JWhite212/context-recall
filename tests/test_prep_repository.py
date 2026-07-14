import time

import pytest

from src.prep.repository import PrepRepository


@pytest.fixture
async def prep_repo(db):
    return PrepRepository(db)


@pytest.mark.asyncio
async def test_create_with_event_link_and_lookup(prep_repo):
    future = time.time() + 3600
    bid = await prep_repo.create(
        content_markdown="brief",
        calendar_event_uid="EK1:1000",
        event_signature="sig-a",
        expires_at=future,
    )
    assert bid
    got = await prep_repo.get_by_calendar_event("EK1:1000")
    assert got is not None and got["content_markdown"] == "brief"
    assert await prep_repo.has_current_for_event("EK1:1000", "sig-a") is True
    assert await prep_repo.has_current_for_event("EK1:1000", "sig-DIFFERENT") is False
    assert await prep_repo.prepared_event_uids() == ["EK1:1000"]
    rows = await prep_repo.list_upcoming()
    assert [r["calendar_event_uid"] for r in rows] == ["EK1:1000"]


@pytest.mark.asyncio
async def test_expired_event_briefing_is_not_current(prep_repo):
    past = time.time() - 10
    await prep_repo.create(
        content_markdown="old",
        calendar_event_uid="EK2:2000",
        event_signature="sig",
        expires_at=past,
    )
    assert await prep_repo.has_current_for_event("EK2:2000", "sig") is False
    assert await prep_repo.get_by_calendar_event("EK2:2000") is None
    assert await prep_repo.prepared_event_uids() == []


@pytest.mark.asyncio
async def test_list_upcoming_dedupes_regenerated_briefings_keeping_newest(prep_repo, db):
    """Regeneration leaves the superseded row until its expiry; list_upcoming
    must not show both (the transient duplicate Prep card)."""
    future = time.time() + 3600
    old_id = await prep_repo.create(
        content_markdown="old brief",
        calendar_event_uid="EK1:1000",
        event_signature="sig-old",
        expires_at=future,
    )
    new_id = await prep_repo.create(
        content_markdown="new brief",
        calendar_event_uid="EK1:1000",
        event_signature="sig-new",
        expires_at=future,
    )
    # Make the ordering unambiguous (creates can share a timestamp).
    await db.conn.execute(
        "UPDATE prep_briefings SET generated_at = generated_at - 100 WHERE id = ?",
        (old_id,),
    )
    await db.conn.commit()

    rows = await prep_repo.list_upcoming()
    assert [r["id"] for r in rows] == [new_id]
    assert rows[0]["content_markdown"] == "new brief"


@pytest.mark.asyncio
async def test_list_upcoming_dedupe_respects_limit_across_events(prep_repo, db):
    """The limit applies to the de-duplicated list, not the raw rows."""
    future = time.time() + 3600
    for i in range(3):
        await prep_repo.create(
            content_markdown=f"brief-{i}",
            calendar_event_uid=f"EK{i}:1000",
            event_signature="sig",
            expires_at=future,
        )
        # Stagger timestamps so ordering is deterministic.
        await db.conn.execute(
            "UPDATE prep_briefings SET generated_at = generated_at + ? "
            "WHERE calendar_event_uid = ?",
            (i, f"EK{i}:1000"),
        )
    await db.conn.commit()

    rows = await prep_repo.list_upcoming(limit=2)
    assert len(rows) == 2
    # Newest first.
    assert [r["calendar_event_uid"] for r in rows] == ["EK2:1000", "EK1:1000"]
