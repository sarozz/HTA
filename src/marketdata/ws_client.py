"""Async WebSocket client for Hyperliquid.

Subscribes to l2Book + trades for a configured symbol list and dispatches
messages to per-channel handlers. On any disconnect, reconnects with
exponential backoff (1s, 2s, 4s, ..., capped at 30s). A watchdog emits a
"stale" event if no message arrives for `stale_seconds`.

Channels surfaced to consumers:
  - "trades"        — list[Trade]
  - "l2Book"        — order book snapshot/diff
  - "stale"         — {"elapsed": float}; emitted at most once per outage
  - "connected"     — {"url": str}
  - "disconnected"  — {"error": str}; emitted before each reconnect

Testability: the websockets library is the default connector but can be
swapped via the `connect` kwarg, which is any callable returning an async
context manager that yields a websocket-like object.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger(__name__)

CHANNEL_TRADES = "trades"
CHANNEL_L2BOOK = "l2Book"
CHANNEL_STALE = "stale"
CHANNEL_CONNECTED = "connected"
CHANNEL_DISCONNECTED = "disconnected"

Handler = Callable[[Any], Awaitable[None] | None]
Connector = Callable[[str], Any]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]


class HyperliquidWsClient:
    def __init__(
        self,
        url: str,
        symbols: list[str],
        *,
        stale_seconds: float = 30.0,
        max_backoff: float = 30.0,
        connect: Connector | None = None,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._url = url
        self._symbols = list(symbols)
        self._stale_seconds = stale_seconds
        self._max_backoff = max_backoff
        self._connect_factory: Connector = connect or websockets.connect
        self._clock = clock
        self._sleep = sleep
        self._handlers: dict[str, list[Handler]] = {}
        self._stop_event = asyncio.Event()
        self._last_msg_at: float = 0.0
        self._stale_emitted = False
        # Observable counters for tests/diagnostics.
        self.connection_count = 0
        self.disconnect_count = 0
        self.backoff_history: list[float] = []

    def on(self, channel: str, handler: Handler) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Main loop. Exits cleanly when `stop()` is called or task cancelled."""
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                # Clean stream end: reset backoff and try again unless stopping.
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.disconnect_count += 1
                logger.warning("ws connection error: %r; retrying in %.2fs", exc, backoff)
                await self._emit(CHANNEL_DISCONNECTED, {"error": repr(exc)})
                if self._stop_event.is_set():
                    break
                self.backoff_history.append(backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2.0, self._max_backoff)

    async def _run_once(self) -> None:
        ctx = self._connect_factory(self._url)
        async with ctx as ws:
            self.connection_count += 1
            self._last_msg_at = self._clock()
            self._stale_emitted = False
            await self._emit(CHANNEL_CONNECTED, {"url": self._url})

            for symbol in self._symbols:
                for sub_type in (CHANNEL_L2BOOK, CHANNEL_TRADES):
                    payload = {
                        "method": "subscribe",
                        "subscription": {"type": sub_type, "coin": symbol},
                    }
                    await ws.send(json.dumps(payload))

            watchdog = asyncio.create_task(self._watchdog())
            try:
                async for raw in ws:
                    self._last_msg_at = self._clock()
                    if self._stale_emitted:
                        self._stale_emitted = False
                    await self._dispatch(raw)
                    if self._stop_event.is_set():
                        break
            finally:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog

    async def _watchdog(self) -> None:
        check_interval = max(self._stale_seconds / 5.0, 0.01)
        while not self._stop_event.is_set():
            await self._sleep(check_interval)
            elapsed = self._clock() - self._last_msg_at
            if elapsed >= self._stale_seconds and not self._stale_emitted:
                self._stale_emitted = True
                await self._emit(CHANNEL_STALE, {"elapsed": elapsed})

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid json from ws (truncated): %r", raw[:200])
            return
        channel = msg.get("channel")
        if not channel:
            return
        await self._emit(channel, msg.get("data"))

    async def _emit(self, channel: str, data: Any) -> None:
        for handler in self._handlers.get(channel, []):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("handler for channel=%s raised", channel)
