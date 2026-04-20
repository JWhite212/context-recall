"""Notification dispatcher — routes notifications to configured channels."""

import logging

from src.api.events import EventBus
from src.notifications.channels import external, in_app, macos
from src.notifications.repository import NotificationRepository
from src.utils.config import NotificationsConfig

logger = logging.getLogger("meetingmind.notifications.dispatcher")


class NotificationDispatcher:
    """Fan-out notification delivery with deduplication and channel routing."""

    def __init__(
        self,
        config: NotificationsConfig,
        repo: NotificationRepository,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._repo = repo
        self._event_bus = event_bus

    async def notify(
        self,
        type: str,
        title: str,
        body: str,
        reference_id: str | None = None,
        channels: list[str] | None = None,
        priority: str = "normal",
        dedupe_window_minutes: int = 60,
    ) -> None:
        """Send a notification across the resolved channels.

        For each channel: check dedup, send, and log to DB.
        """
        if not self._config.enabled:
            return

        resolved = channels if channels is not None else self._default_channels(priority)

        for channel in resolved:
            # Deduplication: skip if a matching notification was recently sent.
            if await self._repo.find_recent(type, reference_id, channel, dedupe_window_minutes):
                logger.debug(
                    "Dedup: skipping %s/%s on channel %s (within %dm window)",
                    type,
                    reference_id,
                    channel,
                    dedupe_window_minutes,
                )
                continue

            sent = await self._send_channel(channel, type, title, body, reference_id)
            status = "sent" if sent else "failed"
            await self._repo.create(
                type=type,
                title=title,
                body=body,
                channel=channel,
                reference_id=reference_id,
                status=status,
            )

    def _default_channels(self, priority: str) -> list[str]:
        """Determine which channels to use based on priority and config.

        - in_app: always (if enabled)
        - macos: normal and high priority (if enabled)
        - webhook / email: high priority only (if enabled)
        """
        channels: list[str] = []
        if self._config.in_app:
            channels.append("in_app")
        if self._config.macos and priority in ("normal", "high"):
            channels.append("macos")
        if self._config.webhook.enabled and priority == "high":
            channels.append("webhook")
        if self._config.email.enabled and priority == "high":
            channels.append("email")
        return channels

    async def _send_channel(
        self,
        channel: str,
        type: str,
        title: str,
        body: str,
        reference_id: str | None,
    ) -> bool:
        """Dispatch to the appropriate channel implementation. Returns True on success."""
        try:
            if channel == "in_app":
                if self._event_bus is not None:
                    await in_app.send(self._event_bus, title, body, type, reference_id)
                    return True
                logger.warning("in_app channel requested but no EventBus available")
                return False
            elif channel == "macos":
                await macos.send(title, body)
                return True
            elif channel == "webhook":
                return await external.send_webhook(self._config.webhook, title, body, type)
            elif channel == "email":
                return await external.send_email(self._config.email, title, body)
            else:
                logger.warning("Unknown notification channel: %s", channel)
                return False
        except Exception:
            logger.exception("Failed to send via channel %s", channel)
            return False
