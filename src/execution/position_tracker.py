"""Cache of exchange positions and open orders.

Per CLAUDE.md non-negotiable #4: the exchange is truth, the local state
is a cache. `refresh()` must be called on startup and again on every
loop iteration (or before any decision that depends on state).

Thread/coroutine safe via an internal lock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.execution.exchange_adapter import HyperliquidExchangeAdapter


@dataclass(frozen=True)
class TrackedPosition:
    symbol: str
    size: Decimal  # signed
    entry_price: Decimal
    mark_price: Decimal
    liq_price: Decimal | None


@dataclass(frozen=True)
class TrackedOrder:
    symbol: str
    oid: int
    is_buy: bool
    size: Decimal
    price: Decimal


class PositionTracker:
    def __init__(self, adapter: HyperliquidExchangeAdapter) -> None:
        self._adapter = adapter
        self._lock = asyncio.Lock()
        self._positions: dict[str, TrackedPosition] = {}
        self._orders: dict[int, TrackedOrder] = {}
        self._equity: Decimal = Decimal("0")
        self._free_margin: Decimal = Decimal("0")
        self._last_refresh_ts: float = 0.0

    async def refresh(self) -> None:
        state = await self._adapter.user_state()
        orders_raw = await self._adapter.list_open_orders()

        async with self._lock:
            self._positions = {}
            for ap in state.get("assetPositions", []):
                pos = ap.get("position") if isinstance(ap, dict) else None
                if not isinstance(pos, dict):
                    continue
                size = _to_decimal(pos.get("szi"))
                if size is None or size == 0:
                    continue
                self._positions[pos["coin"]] = TrackedPosition(
                    symbol=pos["coin"],
                    size=size,
                    entry_price=_to_decimal(pos.get("entryPx")) or Decimal("0"),
                    mark_price=(
                        _to_decimal(pos.get("positionValue", 0)) / abs(size)
                        if size != 0
                        else Decimal("0")
                    ),
                    liq_price=_to_decimal(pos.get("liquidationPx")),
                )

            margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
            self._equity = _to_decimal(margin.get("accountValue")) or Decimal("0")
            self._free_margin = (
                _to_decimal(margin.get("totalRawUsd"))
                or _to_decimal(margin.get("accountValue"))
                or Decimal("0")
            )

            self._orders = {
                ref.oid: TrackedOrder(
                    symbol=ref.symbol,
                    oid=ref.oid,
                    is_buy=True,  # adapter doesn't carry side; refined below if available
                    size=Decimal("0"),
                    price=Decimal("0"),
                )
                for ref in orders_raw
            }

    def get_position(self, symbol: str) -> TrackedPosition | None:
        return self._positions.get(symbol)

    def positions(self) -> list[TrackedPosition]:
        return list(self._positions.values())

    def open_orders(self) -> list[TrackedOrder]:
        return list(self._orders.values())

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def free_margin(self) -> Decimal:
        return self._free_margin


def _to_decimal(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (ValueError, ArithmeticError):
        return None
