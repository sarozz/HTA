"""Tests for HyperliquidWsClient.

The websockets library is replaced by an injected fake connector. Each
fake socket controls exactly when messages arrive and when the connection
"drops" (raises). Tests verify:
  - subscription messages are sent for each (symbol, sub_type)
  - messages are dispatched to per-channel handlers, async or sync
  - invalid JSON is tolerated
  - reconnection happens on a raised exception
  - the backoff sequence is 1, 2, 4, 8, ... capped at max_backoff
  - backoff resets after a clean stream end
  - "stale" is emitted exactly once per outage when no message arrives
    within `stale_seconds`, and re-armed on the next message
  - stop() / cancellation terminate the run loop cleanly
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from src.marketdata.ws_client import (
    CHANNEL_CONNECTED,
    CHANNEL_DISCONNECTED,
    CHANNEL_L2BOOK,
    CHANNEL_STALE,
    CHANNEL_TRADES,
    HyperliquidWsClient,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWS:
    """Fake websocket that yields a fixed message list, then optionally raises.

    Behaves as both an async context manager and an async iterable. Records
    every send() call so tests can assert subscription payloads.
    """

    def __init__(
        self,
        messages: list[str] | None = None,
        raise_at_end: BaseException | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.raise_at_end = raise_at_end
        self.sent: list[str] = []
        self.aenter_count = 0
        self.aexit_count = 0

    async def __aenter__(self) -> FakeWS:
        self.aenter_count += 1
        return self

    async def __aexit__(self, *_: object) -> bool:
        self.aexit_count += 1
        return False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self) -> FakeWS:
        self._iter = iter(self.messages)
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._iter)
        except StopIteration:
            if self.raise_at_end is not None:
                raise self.raise_at_end from None
            raise StopAsyncIteration from None


class StuckWS:
    """Fake websocket that connects but never delivers a message.

    Used to test the stale watchdog: we observe the stale event then
    cancel the run task to unblock.
    """

    async def __aenter__(self) -> StuckWS:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def send(self, msg: str) -> None:
        return None

    def __aiter__(self) -> StuckWS:
        return self

    async def __anext__(self) -> str:
        await asyncio.Event().wait()
        raise StopAsyncIteration  # unreachable


class FakeConnector:
    """Returns the next prepared FakeWS on each call. Raises if exhausted."""

    def __init__(self, sockets: list[Any]) -> None:
        self.sockets = list(sockets)
        self.calls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.calls.append(url)
        if not self.sockets:
            raise RuntimeError(f"FakeConnector exhausted on call #{len(self.calls)}")
        return self.sockets.pop(0)


# ---------------------------------------------------------------------------
# Subscription + dispatch
# ---------------------------------------------------------------------------


async def test_subscribes_to_l2book_and_trades_for_each_symbol() -> None:
    ws = FakeWS()
    connector = FakeConnector([ws])
    client = HyperliquidWsClient(
        "wss://example/ws",
        ["BTC", "ETH"],
        connect=connector,
        sleep=lambda _: asyncio.sleep(0),
    )
    client.on(CHANNEL_CONNECTED, lambda _d: client.stop())

    await asyncio.wait_for(client.run(), timeout=1.0)

    assert client.connection_count == 1
    assert connector.calls == ["wss://example/ws"]
    sent = [json.loads(s) for s in ws.sent]
    assert {("BTC", "l2Book"), ("BTC", "trades"), ("ETH", "l2Book"), ("ETH", "trades")} == {
        (m["subscription"]["coin"], m["subscription"]["type"]) for m in sent
    }
    assert all(m["method"] == "subscribe" for m in sent)


async def test_dispatches_messages_to_channel_handlers() -> None:
    trade_payload = {
        "channel": "trades",
        "data": [{"coin": "BTC", "px": "30000", "sz": "0.01", "time": 1700000000000}],
    }
    book_payload = {"channel": "l2Book", "data": {"coin": "BTC", "levels": []}}
    # trades arrives first, l2Book second. Stop after the l2Book handler so
    # both messages are dispatched before the run loop exits.
    ws = FakeWS(messages=[json.dumps(trade_payload), json.dumps(book_payload)])
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([ws]),
        sleep=lambda _: asyncio.sleep(0),
    )

    trades: list[Any] = []
    books: list[Any] = []
    client.on(CHANNEL_TRADES, lambda data: trades.append(data))

    def on_book(data: Any) -> None:
        books.append(data)
        client.stop()

    client.on(CHANNEL_L2BOOK, on_book)

    await asyncio.wait_for(client.run(), timeout=1.0)

    assert trades == [trade_payload["data"]]
    assert books == [book_payload["data"]]


async def test_async_handler_is_awaited() -> None:
    awaited = asyncio.Event()

    async def slow_handler(_: Any) -> None:
        await asyncio.sleep(0)
        awaited.set()

    ws = FakeWS(messages=[json.dumps({"channel": "trades", "data": []})])
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([ws]),
        sleep=lambda _: asyncio.sleep(0),
    )
    client.on(CHANNEL_TRADES, slow_handler)
    client.on(CHANNEL_CONNECTED, lambda _d: client.stop())

    await asyncio.wait_for(client.run(), timeout=1.0)
    assert awaited.is_set()


async def test_invalid_json_is_tolerated_and_next_message_dispatches() -> None:
    valid = json.dumps({"channel": "trades", "data": [{"px": "1"}]})
    ws = FakeWS(messages=["not-json{", valid])
    received: list[Any] = []
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([ws]),
        sleep=lambda _: asyncio.sleep(0),
    )

    def on_trades(data: Any) -> None:
        received.append(data)
        client.stop()

    client.on(CHANNEL_TRADES, on_trades)

    await asyncio.wait_for(client.run(), timeout=1.0)
    assert received == [[{"px": "1"}]]


async def test_messages_without_channel_are_ignored() -> None:
    # No handler will fire (no "channel" key), so we end the loop by raising
    # at the end of the message stream and stopping on disconnect.
    msg = json.dumps({"id": 1, "method": "subscribe"})
    ws = FakeWS(messages=[msg], raise_at_end=ConnectionError("end-of-stream"))
    received: list[Any] = []
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([ws]),
        sleep=lambda _: asyncio.sleep(0),
    )
    client.on("subscribe", lambda d: received.append(d))
    client.on(CHANNEL_DISCONNECTED, lambda _d: client.stop())

    await asyncio.wait_for(client.run(), timeout=1.0)
    assert received == []


# ---------------------------------------------------------------------------
# Reconnection + backoff
# ---------------------------------------------------------------------------


async def test_reconnects_on_drop_with_exponential_backoff() -> None:
    """Three drops in a row then a clean connect; backoff doubles each time."""
    drops = [
        FakeWS(messages=[], raise_at_end=ConnectionError("drop1")),
        FakeWS(messages=[], raise_at_end=ConnectionError("drop2")),
        FakeWS(messages=[], raise_at_end=ConnectionError("drop3")),
    ]
    final = FakeWS(messages=[])
    connector = FakeConnector([*drops, final])

    sleep_durations: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleep_durations.append(d)
        await asyncio.sleep(0)

    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=connector,
        sleep=fake_sleep,
        max_backoff=30.0,
    )
    disconnects: list[Any] = []
    client.on(CHANNEL_DISCONNECTED, lambda d: disconnects.append(d))

    # Stop only when the FOURTH connect (i.e. the clean one) lands.
    def on_connect(_: Any) -> None:
        if client.connection_count == 4:
            client.stop()

    client.on(CHANNEL_CONNECTED, on_connect)

    await asyncio.wait_for(client.run(), timeout=2.0)

    assert client.connection_count == 4
    assert client.disconnect_count == 3
    assert client.backoff_history == [1.0, 2.0, 4.0]
    # sleep was called for each backoff plus possibly the watchdog ticks; the
    # backoff sleeps must be the three values above, in order.
    backoff_sleeps = [d for d in sleep_durations if d in (1.0, 2.0, 4.0)]
    assert backoff_sleeps == [1.0, 2.0, 4.0]
    assert [d["error"] for d in disconnects] == [
        "ConnectionError('drop1')",
        "ConnectionError('drop2')",
        "ConnectionError('drop3')",
    ]


async def test_backoff_resets_after_clean_stream_end() -> None:
    """A clean exit between drops resets backoff to 1s."""
    seq = [
        FakeWS(messages=[], raise_at_end=ConnectionError("a")),  # drop -> sleep 1
        FakeWS(messages=[]),  # clean end -> reset
        FakeWS(messages=[], raise_at_end=ConnectionError("b")),  # drop -> sleep 1 again
        FakeWS(messages=[]),  # clean end + stop
    ]
    connector = FakeConnector(seq)

    sleep_durations: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleep_durations.append(d)
        await asyncio.sleep(0)

    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=connector,
        sleep=fake_sleep,
    )

    def on_connect(_: Any) -> None:
        if client.connection_count == 4:
            client.stop()

    client.on(CHANNEL_CONNECTED, on_connect)

    await asyncio.wait_for(client.run(), timeout=2.0)

    assert client.connection_count == 4
    # Two drops, both at 1.0 (backoff reset between them).
    assert client.backoff_history == [1.0, 1.0]


async def test_backoff_capped_at_max_backoff() -> None:
    """Cap at max_backoff (5.0). Sequence: 1, 2, 4, 5, 5, 5."""
    drops = [FakeWS(messages=[], raise_at_end=ConnectionError(f"d{i}")) for i in range(6)]
    final = FakeWS(messages=[])
    connector = FakeConnector([*drops, final])

    async def fake_sleep(_: float) -> None:
        await asyncio.sleep(0)

    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=connector,
        sleep=fake_sleep,
        max_backoff=5.0,
    )

    def on_connect(_: Any) -> None:
        if client.connection_count == 7:
            client.stop()

    client.on(CHANNEL_CONNECTED, on_connect)

    await asyncio.wait_for(client.run(), timeout=2.0)

    assert client.backoff_history == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# Stale watchdog
# ---------------------------------------------------------------------------


async def test_stale_event_emitted_after_silence() -> None:
    """Real asyncio.sleep so the watchdog fires; cancel run task on stale."""
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([StuckWS()]),
        stale_seconds=0.05,
    )

    stale_events: list[dict[str, Any]] = []
    stale_seen = asyncio.Event()

    def on_stale(data: Any) -> None:
        stale_events.append(data)
        stale_seen.set()

    client.on(CHANNEL_STALE, on_stale)

    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(stale_seen.wait(), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(stale_events) == 1
    assert stale_events[0]["elapsed"] >= 0.05


async def test_stale_event_not_re_emitted_until_message_then_silence_again() -> None:
    """A message resets the stale flag; later silence triggers stale again."""

    # We need a WS that yields one message after a delay, then goes silent.
    class TwoPhaseWS:
        def __init__(self) -> None:
            self.aenter_count = 0
            self._messages = [
                json.dumps({"channel": "trades", "data": []}),
            ]

        async def __aenter__(self) -> TwoPhaseWS:
            self.aenter_count += 1
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def send(self, _: str) -> None:
            return None

        def __aiter__(self) -> TwoPhaseWS:
            return self

        async def __anext__(self) -> str:
            if self._messages:
                # Wait long enough for the first stale event to fire.
                await asyncio.sleep(0.08)
                return self._messages.pop(0)
            # Now go silent forever (until cancelled).
            await asyncio.Event().wait()
            raise StopAsyncIteration

    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([TwoPhaseWS()]),
        stale_seconds=0.05,
    )
    stale_events: list[dict[str, Any]] = []
    second_stale = asyncio.Event()

    def on_stale(data: Any) -> None:
        stale_events.append(data)
        if len(stale_events) == 2:
            second_stale.set()

    client.on(CHANNEL_STALE, on_stale)

    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(second_stale.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(stale_events) == 2


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


async def test_stop_breaks_loop_after_current_message() -> None:
    """stop() after the first message ends the loop without reconnecting."""
    msg = json.dumps({"channel": "trades", "data": []})
    ws = FakeWS(messages=[msg, msg])
    connector = FakeConnector([ws, FakeWS(messages=[])])
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=connector,
        sleep=lambda _: asyncio.sleep(0),
    )

    def on_trades(_: Any) -> None:
        client.stop()

    client.on(CHANNEL_TRADES, on_trades)

    await asyncio.wait_for(client.run(), timeout=1.0)

    # Only one connection used; the second FakeWS in the queue was never
    # called because stop() ended the run loop after the FakeWS's iteration
    # ended (post the first message and the natural end of its messages list).
    assert connector.calls == ["wss://x/ws"]


async def test_run_propagates_cancellation() -> None:
    client = HyperliquidWsClient(
        "wss://x/ws",
        ["BTC"],
        connect=FakeConnector([StuckWS()]),
        stale_seconds=10.0,  # don't trigger stale during the test
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(0.02)  # let it connect
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
