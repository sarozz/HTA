"""Tests for the dashboard pubsub bus."""

from __future__ import annotations

import asyncio

import pytest

from src.dashboard_api.bus import (
    ALL_CHANNELS,
    CHANNEL_EQUITY,
    CHANNEL_POSITION,
    CHANNEL_TICK,
    Bus,
)


async def test_publish_to_subscribed_channel_delivers() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    bus.publish(CHANNEL_EQUITY, {"equity": "1500"})
    msg = await asyncio.wait_for(sub.get(), timeout=0.1)
    assert msg["type"] == CHANNEL_EQUITY
    assert msg["data"]["equity"] == "1500"
    assert "ts" in msg["data"]  # server stamps if absent


async def test_unsubscribed_channels_do_not_deliver() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    bus.publish(CHANNEL_TICK, {"symbol": "BTC", "mid": 60000})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.05)


async def test_symbol_filter_drops_unmatched() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=[CHANNEL_TICK], symbols=["BTC"])
    bus.publish(CHANNEL_TICK, {"symbol": "ETH", "mid": 3000})
    bus.publish(CHANNEL_TICK, {"symbol": "BTC", "mid": 60000})
    msg = await asyncio.wait_for(sub.get(), timeout=0.1)
    assert msg["data"]["symbol"] == "BTC"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.05)


async def test_no_symbol_filter_passes_all_symbols() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=[CHANNEL_TICK])  # symbols=None
    bus.publish(CHANNEL_TICK, {"symbol": "ETH", "mid": 3000})
    bus.publish(CHANNEL_TICK, {"symbol": "BTC", "mid": 60000})
    a = await asyncio.wait_for(sub.get(), timeout=0.1)
    b = await asyncio.wait_for(sub.get(), timeout=0.1)
    symbols = {a["data"]["symbol"], b["data"]["symbol"]}
    assert symbols == {"BTC", "ETH"}


async def test_unknown_channel_publish_is_silently_dropped() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=list(ALL_CHANNELS))
    bus.publish("definitely_not_a_channel", {"x": 1})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.05)


# ---------------------------------------------------------------------------
# Bounded queue + drop-oldest contract
# ---------------------------------------------------------------------------


async def test_bounded_queue_drops_oldest_when_full() -> None:
    bus = Bus(per_subscriber_queue_size=4)
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    for i in range(10):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    # Queue holds at most 4; the oldest 6 should have been dropped.
    received: list[int] = []
    while True:
        try:
            msg = await asyncio.wait_for(sub.get(), timeout=0.05)
        except asyncio.TimeoutError:
            break
        received.append(int(msg["data"]["equity"]))
    assert len(received) == 4
    # The four kept are the most recent four.
    assert received == [6, 7, 8, 9]
    assert bus.stats["dropped"] >= 6


async def test_publish_never_blocks_even_with_no_consumers_reading() -> None:
    bus = Bus(per_subscriber_queue_size=2)
    await bus.subscribe(channels=[CHANNEL_EQUITY])
    # Publish a thousand messages without any consumer reading. The publish
    # call itself must remain synchronous and non-blocking — that's the
    # contract that protects the trading loop.
    for i in range(1000):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    # If publish blocked, this test wouldn't return.


async def test_slow_client_flag_set_after_threshold_drops() -> None:
    fake_clock = [1000.0]

    def now() -> float:
        return fake_clock[0]

    bus = Bus(
        per_subscriber_queue_size=1,
        slow_threshold=10,
        slow_window_seconds=5.0,
        clock=now,
    )
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    for i in range(20):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    assert sub.slow is True
    assert sub.drops >= 10


async def test_slow_window_drops_outside_window_do_not_count() -> None:
    fake_clock = [1000.0]

    def now() -> float:
        return fake_clock[0]

    bus = Bus(
        per_subscriber_queue_size=1,
        slow_threshold=10,
        slow_window_seconds=5.0,
        clock=now,
    )
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    for i in range(8):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    fake_clock[0] += 10.0  # past the window
    for i in range(8):
        bus.publish(CHANNEL_EQUITY, {"equity": i})
    # Total drops > threshold (16 drops) but no 10 within ANY 5s window
    # except the second batch contributed 8 drops, also <10.
    assert sub.slow is False


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe lifecycle
# ---------------------------------------------------------------------------


async def test_unsubscribe_stops_delivery() -> None:
    bus = Bus()
    sub = await bus.subscribe(channels=[CHANNEL_EQUITY])
    await bus.unsubscribe(sub)
    bus.publish(CHANNEL_EQUITY, {"equity": "post"})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.05)


async def test_subscriber_count_reflects_open_subs() -> None:
    bus = Bus()
    a = await bus.subscribe(channels=[CHANNEL_EQUITY])
    b = await bus.subscribe(channels=[CHANNEL_POSITION])
    assert bus.subscriber_count == 2
    await bus.unsubscribe(a)
    assert bus.subscriber_count == 1
    await bus.unsubscribe(b)
    assert bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def test_stats_track_published_count() -> None:
    bus = Bus()
    await bus.subscribe(channels=[CHANNEL_EQUITY])
    bus.publish(CHANNEL_EQUITY, {"equity": 1})
    bus.publish(CHANNEL_EQUITY, {"equity": 2})
    assert bus.stats["published"] == 2


_ = pytest
