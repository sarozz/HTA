"""WebSocket tests using FastAPI's TestClient.

Covers: token gate at handshake, subscribe contract, snapshot delivery,
delta delivery, rejection of any non-subscribe client message, ping
courtesy reply.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard_api.auth import ENV_VAR
from src.dashboard_api.bus import CHANNEL_EQUITY, Bus
from src.dashboard_api.main import create_app
from src.storage.journal import Journal

TOKEN = "ws-test-token"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, TOKEN)


@pytest.fixture
def app_with_bus(tmp_path) -> tuple[TestClient, Bus, Path]:
    path = tmp_path / "j.sqlite"
    j = Journal(path)
    import asyncio as _asyncio

    async def seed() -> None:
        await j.append("live_start", {"network": "testnet", "symbols": ["BTC"]})
        await j.upsert_state("heartbeat", "_", {"halted": False})

    _asyncio.run(seed())
    bus = Bus()
    app = create_app(journal_path=path, bus=bus)
    return TestClient(app), bus, path


# ---------------------------------------------------------------------------
# Token gate
# ---------------------------------------------------------------------------


def test_ws_rejects_missing_token(app_with_bus) -> None:
    client, _, _ = app_with_bus
    with pytest.raises(Exception):  # noqa: B017 — 422 raises in TestClient
        with client.websocket_connect("/ws"):
            pass


def test_ws_rejects_wrong_token(app_with_bus) -> None:
    client, _, _ = app_with_bus
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_text()  # connect closes before any message


# ---------------------------------------------------------------------------
# Subscribe handshake + snapshot
# ---------------------------------------------------------------------------


def test_ws_subscribe_returns_snapshot_then_streams(app_with_bus) -> None:
    client, bus, _ = app_with_bus
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "subscribe", "channels": [CHANNEL_EQUITY]})
        first = ws.receive_json()
        assert first["type"] == "snapshot"

        bus.publish(CHANNEL_EQUITY, {"equity": "1500"})
        msg = ws.receive_json()
        # Could be a heartbeat (every 5s) or our equity message — accept the equity.
        # In TestClient timing is deterministic; the equity arrives quickly.
        if msg["type"] == "heartbeat":
            msg = ws.receive_json()
        assert msg["type"] == CHANNEL_EQUITY
        assert msg["data"]["equity"] == "1500"


def test_ws_subscribe_with_no_channels_defaults_to_all(app_with_bus) -> None:
    client, bus, _ = app_with_bus
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "subscribe"})  # no channels key
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        bus.publish(CHANNEL_EQUITY, {"equity": "9"})
        # The next message should be either heartbeat or our equity.
        for _ in range(3):
            m = ws.receive_json()
            if m["type"] == CHANNEL_EQUITY:
                break
        else:
            pytest.fail("equity message never arrived")


def test_ws_subscribe_with_unknown_channel_closes(app_with_bus) -> None:
    client, _, _ = app_with_bus
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            ws.send_json({"action": "subscribe", "channels": ["not_a_channel"]})
            # Server sends an error, then closes.
            err = ws.receive_json()
            assert err["type"] == "error"
            ws.receive_json()  # forces the disconnect


def test_ws_first_message_must_be_subscribe(app_with_bus) -> None:
    client, _, _ = app_with_bus
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            ws.send_json({"action": "halt"})  # forbidden
            err = ws.receive_json()
            assert err["type"] == "error"
            ws.receive_json()  # disconnect


# ---------------------------------------------------------------------------
# Read-only contract: post-subscribe messages
# ---------------------------------------------------------------------------


def test_ws_rejects_non_subscribe_after_subscribe(app_with_bus) -> None:
    client, _, _ = app_with_bus
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            ws.send_json({"action": "subscribe", "channels": [CHANNEL_EQUITY]})
            ws.receive_json()  # snapshot
            ws.send_json({"action": "halt", "reason": "stop trading"})
            # Server sends an error, closes.
            for _ in range(5):
                m = ws.receive_json()
                if m["type"] == "error":
                    break
            ws.receive_json()  # forces disconnect


def test_ws_ping_action_replies_with_pong(app_with_bus) -> None:
    client, _, _ = app_with_bus
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "subscribe", "channels": [CHANNEL_EQUITY]})
        ws.receive_json()  # snapshot
        ws.send_json({"action": "ping"})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "pong":
                return
        pytest.fail("never received pong")


# ---------------------------------------------------------------------------
# Channel multiplexing
# ---------------------------------------------------------------------------


def test_ws_subscribe_subset_only_delivers_subset(app_with_bus) -> None:
    client, bus, _ = app_with_bus
    from src.dashboard_api.bus import CHANNEL_TICK

    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "subscribe", "channels": [CHANNEL_EQUITY]})
        ws.receive_json()  # snapshot
        bus.publish(CHANNEL_TICK, {"symbol": "BTC", "mid": 60000})  # not subscribed
        bus.publish(CHANNEL_EQUITY, {"equity": 1500})
        for _ in range(3):
            m = ws.receive_json()
            if m["type"] == CHANNEL_EQUITY:
                return
            if m["type"] == CHANNEL_TICK:
                pytest.fail("received tick message we didn't subscribe to")


_ = (asyncio, json, time)
