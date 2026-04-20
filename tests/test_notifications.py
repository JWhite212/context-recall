"""Tests for the notification dispatcher and repository."""

import pytest

from src.db.database import Database
from src.notifications.dispatcher import NotificationDispatcher
from src.notifications.repository import NotificationRepository
from src.utils.config import NotificationsConfig


@pytest.fixture
async def notif_repo(db: Database):
    return NotificationRepository(db)


@pytest.fixture
async def dispatcher(db, notif_repo, event_bus):
    config = NotificationsConfig(enabled=True, in_app=True, macos=False)
    return NotificationDispatcher(config=config, repo=notif_repo, event_bus=event_bus)


@pytest.mark.asyncio
async def test_notify_stores_in_db(dispatcher, notif_repo):
    await dispatcher.notify(
        type="reminder", title="Test reminder", body="Do the thing", reference_id="item-1"
    )
    items = await notif_repo.list_notifications(limit=10)
    assert len(items) == 1
    assert items[0]["title"] == "Test reminder"
    assert items[0]["type"] == "reminder"
    assert items[0]["status"] == "sent"


@pytest.mark.asyncio
async def test_notify_deduplicates(dispatcher, notif_repo):
    await dispatcher.notify(type="overdue", title="Overdue", body="Past due", reference_id="item-2")
    await dispatcher.notify(type="overdue", title="Overdue", body="Past due", reference_id="item-2")
    items = await notif_repo.list_notifications(limit=10)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_notify_disabled_does_nothing(db, notif_repo, event_bus):
    config = NotificationsConfig(enabled=False)
    d = NotificationDispatcher(config=config, repo=notif_repo, event_bus=event_bus)
    await d.notify(type="reminder", title="Ignored", body="")
    items = await notif_repo.list_notifications(limit=10)
    assert len(items) == 0
