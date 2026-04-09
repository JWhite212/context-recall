"""Tests for EventBus dispatch."""

import asyncio
import time

import pytest

from src.api.events import EventBus


def test_sync_callback():
    bus = EventBus()
    received = []
    bus.subscribe_sync(lambda e: received.append(e))
    bus.emit({"type": "test.event", "data": 42})
    assert len(received) == 1
    assert received[0]["type"] == "test.event"
    assert received[0]["data"] == 42


def test_timestamp_injected():
    bus = EventBus()
    received = []
    bus.subscribe_sync(lambda e: received.append(e))
    before = time.time()
    bus.emit({"type": "test"})
    after = time.time()
    assert "timestamp" in received[0]
    assert before <= received[0]["timestamp"] <= after


def test_timestamp_not_overwritten():
    bus = EventBus()
    received = []
    bus.subscribe_sync(lambda e: received.append(e))
    bus.emit({"type": "test", "timestamp": 123.0})
    assert received[0]["timestamp"] == 123.0


def test_error_isolation():
    """A failing callback should not prevent others from running."""
    bus = EventBus()
    results = []

    def bad_callback(e):
        raise RuntimeError("boom")

    def good_callback(e):
        results.append(e)

    bus.subscribe_sync(bad_callback)
    bus.subscribe_sync(good_callback)
    bus.emit({"type": "test"})
    assert len(results) == 1


def test_unsubscribe_sync():
    bus = EventBus()
    received = []
    def cb(e):
        received.append(e)
    bus.subscribe_sync(cb)
    bus.emit({"type": "first"})
    bus.unsubscribe_sync(cb)
    bus.emit({"type": "second"})
    assert len(received) == 1


@pytest.mark.asyncio
async def test_async_callback():
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)

    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe_async(handler)
    bus.emit({"type": "async.test"})

    # Give the coroutine time to be scheduled and run.
    await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received[0]["type"] == "async.test"


@pytest.mark.asyncio
async def test_unsubscribe_async():
    bus = EventBus()
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)

    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe_async(handler)
    bus.emit({"type": "first"})
    await asyncio.sleep(0.1)
    bus.unsubscribe_async(handler)
    bus.emit({"type": "second"})
    await asyncio.sleep(0.1)
    assert len(received) == 1


def test_emit_without_loop():
    """Emitting without an event loop set should not raise."""
    bus = EventBus()
    received = []
    bus.subscribe_sync(lambda e: received.append(e))
    bus.emit({"type": "no_loop"})
    assert len(received) == 1
