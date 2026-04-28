"""WebSocket multiplexer.

Single endpoint /ws. Each client opens one connection, authenticates
with ?token=..., subscribes to a list of channels, and receives a
snapshot followed by a stream of deltas.

Server -> client message shape:
    {"type": <channel>, "data": {...}}
where <channel> is one of bus.ALL_CHANNELS plus "snapshot" (sent once
on subscribe) and "error" (sent and connection closed on protocol
error).

Client -> server messages: ONLY {"action": "subscribe", "channels": [...],
"symbols": [...]} is accepted. Any other message terminates the
connection. WebSocket auth happens before this method is called (the
route handler in main.py validates the token from the query param).

Server-side heartbeat every 5s. Slow clients (per bus.Subscription.slow)
are disconnected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from src.dashboard_api.bus import ALL_CHANNELS, Bus, Subscription
from src.dashboard_api.queries import Queries

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 5.0
SLOW_CLIENT_CHECK_INTERVAL = 1.0


async def handle_ws(websocket: WebSocket, bus: Bus, queries: Queries) -> None:
    """Run the lifetime of a single WS connection.

    Auth is validated by the caller (token query param). This function
    accepts the connection, processes the subscribe message, and runs
    the read/write loop.
    """
    await websocket.accept()
    try:
        sub = await _await_subscribe(websocket, bus)
    except _BadProtocolError as exc:
        await _send_error(websocket, str(exc))
        await websocket.close(code=1008)
        return
    except WebSocketDisconnect:
        return

    # Send the initial snapshot synchronously so the client can render
    # before any deltas arrive.
    try:
        snapshot = queries.snapshot()
        await websocket.send_json({"type": "snapshot", "data": snapshot})
    except Exception:
        logger.exception("snapshot failed at WS open")
        await _send_error(websocket, "snapshot failed")
        await websocket.close(code=1011)
        await bus.unsubscribe(sub)
        return

    sender = asyncio.create_task(_send_loop(websocket, sub, bus), name="ws-send")
    receiver = asyncio.create_task(_recv_loop(websocket), name="ws-recv")
    heartbeat = asyncio.create_task(_heartbeat_loop(websocket), name="ws-heartbeat")
    watchdog = asyncio.create_task(_slow_client_watchdog(websocket, sub), name="ws-watchdog")

    done, pending = await asyncio.wait(
        {sender, receiver, heartbeat, watchdog},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await bus.unsubscribe(sub)
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
            logger.warning("ws task failed: %r", exc)
    try:
        await websocket.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sub-loops
# ---------------------------------------------------------------------------


class _BadProtocolError(RuntimeError):
    pass


async def _await_subscribe(websocket: WebSocket, bus: Bus) -> Subscription:
    raw = await websocket.receive_text()
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _BadProtocolError("first message must be JSON") from exc

    action = msg.get("action") if isinstance(msg, dict) else None
    if action != "subscribe":
        raise _BadProtocolError("first message must be {'action': 'subscribe', ...}")
    channels = msg.get("channels") or list(ALL_CHANNELS)
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise _BadProtocolError("channels must be a list of strings")
    unknown = [c for c in channels if c not in ALL_CHANNELS]
    if unknown:
        raise _BadProtocolError(f"unknown channels: {unknown}")

    symbols_raw = msg.get("symbols")
    if symbols_raw is None:
        symbols: list[str] | None = None
    elif isinstance(symbols_raw, list) and all(isinstance(s, str) for s in symbols_raw):
        symbols = symbols_raw
    else:
        raise _BadProtocolError("symbols must be a list of strings or omitted")

    return await bus.subscribe(channels=channels, symbols=symbols)


async def _send_loop(websocket: WebSocket, sub: Subscription, bus: Bus) -> None:
    try:
        while not sub.closed:
            message = await sub.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        return
    finally:
        await bus.unsubscribe(sub)


async def _recv_loop(websocket: WebSocket) -> None:
    """Reject any client message after the initial subscribe."""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        # Any post-subscribe message terminates the connection per the
        # read-only contract.
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await _send_error(websocket, "invalid json")
            await websocket.close(code=1003)
            return
        action = msg.get("action") if isinstance(msg, dict) else None
        if action == "ping":
            # Allow pings as a goodwill courtesy; reply with pong.
            try:
                await websocket.send_json({"type": "pong", "data": {"ts": time.time()}})
            except WebSocketDisconnect:
                return
            continue
        await _send_error(websocket, "this is a read-only stream; only subscribe is allowed")
        await websocket.close(code=1008)
        return


async def _heartbeat_loop(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send_json({"type": "heartbeat", "data": {"ts": time.time()}})
        except WebSocketDisconnect:
            return
        except Exception:
            return


async def _slow_client_watchdog(websocket: WebSocket, sub: Subscription) -> None:
    while not sub.closed:
        await asyncio.sleep(SLOW_CLIENT_CHECK_INTERVAL)
        if sub.slow:
            logger.warning("dropping slow WS client (drops=%d)", sub.drops)
            try:
                await _send_error(websocket, "slow consumer; disconnecting")
            except Exception:
                pass
            try:
                await websocket.close(code=1013)
            except Exception:
                pass
            return


async def _send_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "data": {"message": message}})
    except Exception:
        pass


def _ws_message(channel: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": channel, "data": data}
