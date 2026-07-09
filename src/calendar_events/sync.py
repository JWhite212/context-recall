"""Apply a fetched window of calendar events to the mirror table."""

import logging

from src.calendar_events.reader import CalendarEvent
from src.calendar_events.repository import CalendarEventRepository

logger = logging.getLogger("contextrecall.calendar_events")


class CalendarSyncJob:
    """Upsert a fetched window of events and prune those that vanished from it."""

    def __init__(self, repo: CalendarEventRepository) -> None:
        self._repo = repo

    async def apply(
        self, window_start: float, window_end: float, events: list[CalendarEvent]
    ) -> int:
        for event in events:
            await self._repo.upsert(event)
        keep = {e.event_uid for e in events}
        removed = await self._repo.prune_window(window_start, window_end, keep)
        logger.debug(
            "Calendar sync applied: %d upserted, %d pruned (window %.0f-%.0f)",
            len(events),
            removed,
            window_start,
            window_end,
        )
        return len(events)
