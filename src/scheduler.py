"""Lightweight asyncio scheduler for MeetingMind background tasks.

Runs periodic jobs (analytics refresh, prep triggers, reminder checks)
on a single asyncio task. Jobs that raise are logged but don't crash
the scheduler or other jobs.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("meetingmind.scheduler")

Job = Callable[[], Awaitable[None]]


@dataclass
class _RegisteredJob:
    name: str
    func: Job
    interval_seconds: float
    last_run: float = 0.0


class Scheduler:
    """Background scheduler that ticks at an interval derived from registered jobs."""

    def __init__(self) -> None:
        self._jobs: list[_RegisteredJob] = []
        self._task: asyncio.Task | None = None
        self._running = False

    def register(self, name: str, func: Job, interval_seconds: float) -> None:
        self._jobs.append(
            _RegisteredJob(
                name=name,
                func=func,
                interval_seconds=interval_seconds,
            )
        )

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Scheduler started with %d jobs", len(self._jobs))

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                now = time.time()
                for job in self._jobs:
                    if now - job.last_run >= job.interval_seconds:
                        job.last_run = now
                        try:
                            await job.func()
                        except Exception:
                            logger.exception("Scheduler job '%s' failed", job.name)
                tick = min((j.interval_seconds for j in self._jobs), default=1.0)
                await asyncio.sleep(max(tick, 0.05))
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Scheduler stopped")
