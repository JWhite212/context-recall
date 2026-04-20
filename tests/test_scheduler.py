import asyncio

import pytest

from src.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_runs_registered_jobs():
    called = []

    async def job():
        called.append(1)

    scheduler = Scheduler()
    scheduler.register("test_job", job, interval_seconds=0.1)
    scheduler.start()
    await asyncio.sleep(0.35)
    scheduler.stop()
    assert len(called) >= 2


@pytest.mark.asyncio
async def test_scheduler_handles_job_errors():
    good_calls = []

    async def bad_job():
        raise RuntimeError("oops")

    async def good_job():
        good_calls.append(1)

    scheduler = Scheduler()
    scheduler.register("bad", bad_job, interval_seconds=0.1)
    scheduler.register("good", good_job, interval_seconds=0.1)
    scheduler.start()
    await asyncio.sleep(0.35)
    scheduler.stop()
    assert len(good_calls) >= 2


@pytest.mark.asyncio
async def test_scheduler_stop_is_idempotent():
    scheduler = Scheduler()
    scheduler.stop()  # Should not raise
