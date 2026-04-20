"""In-app notification via WebSocket event bus."""

import logging

from src.api.events import EventBus

logger = logging.getLogger("meetingmind.notifications.in_app")


async def send(
    event_bus: EventBus,
    title: str,
    body: str,
    type: str,
    reference_id: str | None,
) -> None:
    event_bus.emit(
        {
            "type": "notification",
            "notification_type": type,
            "title": title,
            "body": body,
            "reference_id": reference_id,
        }
    )
    logger.debug("In-app notification sent: %s", title)
