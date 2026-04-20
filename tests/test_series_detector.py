import json
from datetime import datetime, timedelta, timezone

import pytest

from src.db.database import Database
from src.series.detector import HeuristicSeriesDetector
from src.series.repository import SeriesRepository
from src.utils.config import SeriesConfig


@pytest.fixture
async def series_repo(db: Database):
    return SeriesRepository(db)


@pytest.fixture
async def detector(db: Database, repo, series_repo):
    config = SeriesConfig(min_meetings_for_series=2)  # Lower for testing
    return HeuristicSeriesDetector(config=config, meeting_repo=repo, series_repo=series_repo)


@pytest.mark.asyncio
async def test_detects_recurring_by_attendees_and_day(detector, repo, series_repo):
    base = datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)  # Monday 2pm
    attendees = json.dumps(
        [{"name": "Alice", "email": "alice@co.com"}, {"name": "Bob", "email": "bob@co.com"}]
    )
    for i in range(3):
        meeting_time = base + timedelta(weeks=i)
        mid = await repo.create_meeting(started_at=meeting_time.timestamp())
        await repo.update_meeting(
            mid,
            title="Weekly Sync",
            status="complete",
            attendees_json=attendees,
            duration_seconds=1800,
            ended_at=meeting_time.timestamp() + 1800,
        )

    new_series = await detector.detect()
    assert len(new_series) >= 1
    series = await series_repo.list_all()
    assert len(series) == 1
    meetings = await series_repo.get_meetings(series[0]["id"])
    assert len(meetings) == 3


@pytest.mark.asyncio
async def test_does_not_group_dissimilar_meetings(detector, repo, series_repo):
    base = datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)
    for i in range(3):
        meeting_time = base + timedelta(weeks=i)
        attendees = json.dumps([{"name": f"Person{i}", "email": f"p{i}@co.com"}])
        mid = await repo.create_meeting(started_at=meeting_time.timestamp())
        await repo.update_meeting(
            mid,
            title=f"Meeting {i}",
            status="complete",
            attendees_json=attendees,
            duration_seconds=1800,
            ended_at=meeting_time.timestamp() + 1800,
        )

    new_series = await detector.detect()
    assert len(new_series) == 0
