"""Tests for PositionTracker — caches exchange positions/orders."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.position_tracker import PositionTracker
from src.risk.kill_switch import OpenOrderRef


def _make_adapter(user_state: dict, open_orders: list[OpenOrderRef]):
    a = MagicMock()
    a.user_state = AsyncMock(return_value=user_state)
    a.list_open_orders = AsyncMock(return_value=open_orders)
    return a


async def test_refresh_populates_positions_and_equity() -> None:
    state = {
        "marginSummary": {"accountValue": "1500.00", "totalRawUsd": "1500.00"},
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.5",
                    "entryPx": "60000",
                    "positionValue": "30000",
                    "liquidationPx": "55000",
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "-1.0",
                    "entryPx": "3000",
                    "positionValue": "3000",
                    "liquidationPx": None,
                }
            },
        ],
    }
    adapter = _make_adapter(state, [OpenOrderRef("BTC", 1), OpenOrderRef("ETH", 2)])
    tracker = PositionTracker(adapter)
    await tracker.refresh()

    assert tracker.equity == Decimal("1500.00")
    btc = tracker.get_position("BTC")
    eth = tracker.get_position("ETH")
    assert btc is not None and btc.size == Decimal("0.5")
    assert eth is not None and eth.size == Decimal("-1.0")
    assert btc.entry_price == Decimal("60000")
    assert {o.oid for o in tracker.open_orders()} == {1, 2}


async def test_refresh_drops_zero_size_positions() -> None:
    state = {
        "marginSummary": {"accountValue": "1500"},
        "assetPositions": [{"position": {"coin": "BTC", "szi": "0", "entryPx": "100"}}],
    }
    adapter = _make_adapter(state, [])
    tracker = PositionTracker(adapter)
    await tracker.refresh()
    assert tracker.get_position("BTC") is None
    assert tracker.positions() == []


async def test_get_position_unknown_returns_none() -> None:
    adapter = _make_adapter({"marginSummary": {"accountValue": "1500"}, "assetPositions": []}, [])
    tracker = PositionTracker(adapter)
    await tracker.refresh()
    assert tracker.get_position("DOGE") is None


async def test_refresh_replaces_previous_state() -> None:
    state1 = {
        "marginSummary": {"accountValue": "1500"},
        "assetPositions": [{"position": {"coin": "BTC", "szi": "1", "entryPx": "100"}}],
    }
    state2 = {
        "marginSummary": {"accountValue": "1600"},
        "assetPositions": [{"position": {"coin": "ETH", "szi": "2", "entryPx": "200"}}],
    }
    adapter = MagicMock()
    adapter.user_state = AsyncMock(side_effect=[state1, state2])
    adapter.list_open_orders = AsyncMock(side_effect=[[], []])
    tracker = PositionTracker(adapter)

    await tracker.refresh()
    assert tracker.get_position("BTC") is not None
    assert tracker.equity == Decimal("1500")

    await tracker.refresh()
    assert tracker.get_position("BTC") is None
    assert tracker.get_position("ETH") is not None
    assert tracker.equity == Decimal("1600")


async def test_refresh_handles_missing_margin_summary() -> None:
    state = {"assetPositions": []}
    adapter = _make_adapter(state, [])
    tracker = PositionTracker(adapter)
    await tracker.refresh()
    assert tracker.equity == Decimal("0")
    assert tracker.positions() == []


_ = pytest
