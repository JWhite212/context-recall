import json
from datetime import datetime, timezone

import pytest

from src.action_items.repository import ActionItemRepository
from src.analytics.engine import AnalyticsEngine
from src.analytics.repository import AnalyticsRepository
from src.db.database import Database
from src.utils.config import AnalyticsConfig


@pytest.fixture
async def analytics_repo(db: Database):
    return AnalyticsRepository(db)


@pytest.fixture
async def ai_repo(db: Database):
    return ActionItemRepository(db)


@pytest.fixture
async def engine(db: Database, repo, analytics_repo, ai_repo):
    return AnalyticsEngine(
        config=AnalyticsConfig(),
        meeting_repo=repo,
        analytics_repo=analytics_repo,
        action_item_repo=ai_repo,
    )


@pytest.mark.asyncio
async def test_compute_daily_analytics(engine, repo, analytics_repo):
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    attendees = json.dumps([{"name": "A", "email": "a@co.com"}])
    m1 = await repo.create_meeting(started_at=ts)
    await repo.update_meeting(
        m1,
        status="complete",
        duration_seconds=1800,
        word_count=500,
        attendees_json=attendees,
        ended_at=ts + 1800,
    )
    m2 = await repo.create_meeting(started_at=ts + 3600)
    await repo.update_meeting(
        m2,
        status="complete",
        duration_seconds=3600,
        word_count=1000,
        attendees_json=attendees,
        ended_at=ts + 7200,
    )
    await engine.refresh_period("daily", now.strftime("%Y-%m-%d"))
    row = await analytics_repo.get_period("daily", now.strftime("%Y-%m-%d"))
    assert row is not None
    assert row["total_meetings"] == 2
    assert row["total_duration_minutes"] == 90
    assert row["total_words"] == 1500


@pytest.mark.asyncio
async def test_load_score_no_data(engine):
    score = await engine.compute_load_score()
    assert score["label"] == "No data"


@pytest.mark.asyncio
async def test_most_met_people(engine, repo):
    attendees = json.dumps(
        [{"name": "Alice", "email": "a@co.com"}, {"name": "Bob", "email": "b@co.com"}]
    )
    for i in range(3):
        mid = await repo.create_meeting(started_at=1700000000 + i * 100)
        await repo.update_meeting(mid, status="complete", attendees_json=attendees)
    people = await engine.get_most_met_people()
    assert len(people) == 2
    assert people[0]["meeting_count"] == 3
