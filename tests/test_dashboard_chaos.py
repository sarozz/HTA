"""Chaos test: a slow WebSocket client must not block the bus.

A producer publishes a high-rate stream to the bus. One subscriber
("fast") drains its queue at full speed. Another subscriber ("slow")
never reads. The slow subscriber's queue fills up and triggers the
slow flag, but the producer's publish() call must remain
non-blocking and the fast subscriber must keep receiving without
backpressure from the slow one.

This is the contract that protects the trading loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.dashboard_api.bus import CHANNEL_EQUITY, Bus


async def test_slow_subscriber_does_not_block_publisher_or_fast_subscribers() -> None:
    """Three guarantees, in priority order:

    1. publish() returns synchronously regardless of slow subscribers
       (the trading-loop-protection contract).
    2. The slow subscriber's queue is bounded and the bus marks it slow.
    3. The fast subscriber receives the most recent messages, not gated
       by the slow one's lag.
    """
    bus = Bus(per_subscriber_queue_size=10, slow_threshold=20, slow_window_seconds=5.0)
    fast = await bus.subscribe(channels=[CHANNEL_EQUITY])
    slow = await bus.subscribe(channels=[CHANNEL_EQUITY])

    # Burst-publish 500 messages WITHOUT awaiting anything in between. Both
    # fast and slow subscribers' queues fill up; both should hit drop-oldest.
    publish_started = time.monotonic()
    for i in range(500):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    publish_elapsed = time.monotonic() - publish_started

    # Guarantee 1: publish was non-blocking.
    assert (
        publish_elapsed < 1.0
    ), f"publish took {publish_elapsed:.3f}s — slow subscriber blocked the bus"

    # Guarantee 2: the slow flag is set; queue is bounded.
    assert slow.queue.qsize() == 10
    assert slow.slow is True
    assert slow.drops > 0

    # Guarantee 3: fast subscriber holds the latest 10 messages (drop-oldest).
    drained: list[int] = []
    while True:
        try:
            msg = await asyncio.wait_for(fast.get(), timeout=0.05)
        except asyncio.TimeoutError:
            break
        drained.append(int(msg["data"]["equity"]))
    assert len(drained) == 10
    assert drained == list(range(490, 500))  # latest 10
    assert bus.stats["dropped"] >= 980  # both subs dropped ~490 each


async def test_publish_remains_synchronous_under_a_thousand_subscribers() -> None:
    """Smoke: 1000 subscribers; publish stays trivial on its hot path."""
    bus = Bus(per_subscriber_queue_size=4)
    for _ in range(1000):
        await bus.subscribe(channels=[CHANNEL_EQUITY])
    started = time.monotonic()
    for i in range(50):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"publish to 1000 subs took {elapsed:.3f}s"


_ = pytest
