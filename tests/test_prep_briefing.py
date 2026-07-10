import pytest

from src.action_items.repository import ActionItemRepository
from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository
from src.series.repository import SeriesRepository
from src.utils.config import PrepConfig, SummarisationConfig


@pytest.fixture
async def generator(db, repo):
    gen = PrepBriefingGenerator(
        config=PrepConfig(),
        summarisation_config=SummarisationConfig(),
        meeting_repo=repo,
        action_item_repo=ActionItemRepository(db),
        series_repo=SeriesRepository(db),
        prep_repo=PrepRepository(db),
    )
    # Stub the LLM: no real model / network.
    gen._summariser.chat = lambda system, user: "## Prep\nstubbed briefing"
    return gen


@pytest.mark.asyncio
async def test_generate_links_calendar_event(generator, db):
    prep_repo = PrepRepository(db)
    future = 9999999999.0
    bid = await generator.generate(
        title="Weekly sync",
        attendees=["a@x.com"],
        attendee_names=["Alice"],
        calendar_event_uid="EK1:1000",
        event_signature="sig-a",
        expires_at=future,
    )
    assert bid
    got = await prep_repo.get_by_calendar_event("EK1:1000")
    assert got is not None
    assert got["event_signature"] == "sig-a"
    assert "stubbed briefing" in got["content_markdown"]


@pytest.mark.asyncio
async def test_generate_falls_back_on_llm_error(generator, db):
    def _boom(system, user):
        raise RuntimeError("llm down")

    generator._summariser.chat = _boom
    await generator.generate(
        title="Weekly sync",
        attendees=["a@x.com"],
        attendee_names=["Alice"],
        calendar_event_uid="EK9:9000",
        event_signature="sig",
        expires_at=9999999999.0,
    )
    got = await PrepRepository(db).get_by_calendar_event("EK9:9000")
    assert got is not None  # fallback briefing still created + linked
    assert "Weekly sync" in got["content_markdown"]
