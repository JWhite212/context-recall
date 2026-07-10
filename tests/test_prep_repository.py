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
