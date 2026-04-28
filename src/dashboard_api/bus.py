"""In-memory pub/sub bus shared between the trading loop and the dashboard API.

Both producers (trading loop) and consumers (WS clients via dashboard_api)
run in the same Python process. This module is the only shared mutable
state. Imports are intentionally minimal: stdlib only, no SDK, no I/O.

Contract:
  - publish(channel, data) is non-blocking and never raises.
  - Each subscriber owns a bounded asyncio.Queue (default maxsize=1000).
  - When a subscriber's queue is full, the OLDEST message is dropped to
    make room. The slow client never back-pressures the trading loop.
  - A subscriber that has had to drop more than `slow_threshold` messages
    in `slow_window_seconds` is marked slow. The WS layer reads this
    flag and disconnects the client; the bus itself never closes a
    subscriber on a slow caller's behalf.
  - Channels are simple strings; no wildcards. Consumers subscribe to
    an explicit list of channels.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Channel name constants — keep in one place so producers and consumers
# never accidentally diverge.
CHANNEL_EQUITY = "equity"
CHANNEL_TICK = "tick"
CHANNEL_POSITION = "position"
CHANNEL_FILL = "fill"
CHANNEL_SIGNAL = "signal"
CHANNEL_RISK_EVENT = "risk_event"
CHANNEL_ORDERBOOK = "orderbook"
CHANNEL_HEARTBEAT = "heartbeat"

ALL_CHANNELS: tuple[str, ...] = (
    CHANNEL_EQUITY,
    CHANNEL_TICK,
    CHANNEL_POSITION,
    CHANNEL_FILL,
    CHANNEL_SIGNAL,
    CHANNEL_RISK_EVENT,
    CHANNEL_ORDERBOOK,
    CHANNEL_HEARTBEAT,
)


@dataclass
class Subscription:
    """One consumer's view onto the bus."""

    channels: frozenset[str]
    symbols: frozenset[str] | None  # None = all symbols
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    drops: int = 0
    drop_timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=200))
    slow: bool = False
    closed: bool = False

    async def get(self) -> dict[str, Any]:
        return await self.queue.get()

    def matches(self, channel: str, symbol: str | None) -> bool:
        if channel not in self.channels:
            return False
        if self.symbols is None or symbol is None:
            return True
        return symbol in self.symbols


class Bus:
    """Bounded fan-out pubsub."""

    def __init__(
        self,
        *,
        per_subscriber_queue_size: int = 1000,
        slow_threshold: int = 100,
        slow_window_seconds: float = 10.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._queue_size = per_subscriber_queue_size
        self._slow_threshold = slow_threshold
        self._slow_window = slow_window_seconds
        self._clock = clock
        self._subs: list[Subscription] = []
        self._lock = asyncio.Lock()
        self._published_count = 0
        self._dropped_count = 0

    @property
    def subscriber_count(self) -> int:
        return sum(1 for s in self._subs if not s.closed)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": self.subscriber_count,
            "published": self._published_count,
            "dropped": self._dropped_count,
        }

    async def subscribe(
        self,
        channels: Iterable[str],
        symbols: Iterable[str] | None = None,
    ) -> Subscription:
        sub = Subscription(
            channels=frozenset(channels),
            symbols=frozenset(symbols) if symbols is not None else None,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        async with self._lock:
            self._subs.append(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            sub.closed = True
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Fan-out non-blocking. Safe to call from the trading loop.

        `data` should be plain JSON-serialisable. The bus stamps a
        server-side ts if not already present.
        """
        if channel not in ALL_CHANNELS:
            # Unknown channel — log once and drop. Keeps producers honest.
            logger.warning("publish to unknown channel: %s", channel)
            return
        message = {"type": channel, "data": dict(data)}
        if "ts" not in message["data"]:
            message["data"]["ts"] = time.time()
        symbol = message["data"].get("symbol")
        self._published_count += 1
        for sub in self._subs:
            if sub.closed or not sub.matches(channel, symbol):
                continue
            self._deliver(sub, message)

    def _deliver(self, sub: Subscription, message: dict[str, Any]) -> None:
        try:
            sub.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        # Queue full — drop oldest, push newest. Never blocks the producer.
        try:
            sub.queue.get_nowait()
            self._dropped_count += 1
            sub.drops += 1
            now = self._clock()
            sub.drop_timestamps.append(now)
            cutoff = now - self._slow_window
            recent_drops = sum(1 for t in sub.drop_timestamps if t >= cutoff)
            if recent_drops >= self._slow_threshold:
                sub.slow = True
                logger.warning(
                    "subscriber marked slow: %d drops in last %.1fs",
                    recent_drops,
                    self._slow_window,
                )
        except asyncio.QueueEmpty:
            return
        try:
            sub.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Should be impossible right after a get, but never raise upstream.
            self._dropped_count += 1
