"""Async adapter wrapping Hyperliquid's sync Info+Exchange clients.

The Hyperliquid Python SDK is synchronous and uses requests internally.
This adapter exposes async methods (via asyncio.to_thread) so the rest
of the system can `await` exchange calls without blocking the loop.

Two related Protocols satisfied here:
  - src.risk.kill_switch.ExchangeAdapter (list/cancel/close)
  - the broader interface used by PositionTracker, OrderRouter, and
    Reconciler (placing orders, querying user_state, etc.)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from src.risk.kill_switch import OpenOrderRef, PositionSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderResult:
    """Outcome of submitting a single order."""

    accepted: bool
    oid: int | None
    error_code: str | None  # e.g. "alo_would_take" if ALO would have taken
    raw: Any


def _is_alo_rejected(raw: Any) -> bool:
    """Is this Hyperliquid response an ALO 'would have taken' rejection?

    Shape (per SDK docs):
      {"status": "ok", "response": {"data": {"statuses": [
          {"error": "Order would immediately match (post only)..."}
      ]}}}
    """
    try:
        statuses = raw["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return False
    if not statuses:
        return False
    err = statuses[0].get("error", "") if isinstance(statuses[0], dict) else ""
    err_l = str(err).lower()
    return "post only" in err_l or "post-only" in err_l or "would immediately match" in err_l


def _extract_oid(raw: Any) -> int | None:
    try:
        statuses = raw["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return None
    if not statuses:
        return None
    s = statuses[0]
    if isinstance(s, dict):
        if "resting" in s and isinstance(s["resting"], dict):
            return int(s["resting"].get("oid"))
        if "filled" in s and isinstance(s["filled"], dict):
            return int(s["filled"].get("oid"))
    return None


class HyperliquidExchangeAdapter:
    """Single async surface over Info + Exchange for testnet/mainnet."""

    def __init__(self, info: Any, exchange: Any, address: str) -> None:
        self._info = info
        self._exchange = exchange
        self._address = address

    # --- KillSwitch ExchangeAdapter Protocol -----------------------------

    async def list_open_orders(self) -> list[OpenOrderRef]:
        orders = await asyncio.to_thread(self._info.open_orders, self._address)
        out: list[OpenOrderRef] = []
        for o in orders or []:
            try:
                out.append(OpenOrderRef(symbol=o["coin"], oid=int(o["oid"])))
            except (KeyError, TypeError, ValueError):
                logger.warning("ill-formed open order: %r", o)
        return out

    async def list_positions(self) -> list[PositionSnapshot]:
        state = await asyncio.to_thread(self._info.user_state, self._address)
        out: list[PositionSnapshot] = []
        for ap in state.get("assetPositions", []) if isinstance(state, dict) else []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not isinstance(pos, dict):
                continue
            try:
                out.append(
                    PositionSnapshot(
                        symbol=pos["coin"],
                        size=Decimal(str(pos.get("szi", "0"))),
                    )
                )
            except (KeyError, ValueError):
                logger.warning("ill-formed position: %r", pos)
        return out

    async def cancel(self, symbol: str, oid: int) -> None:
        await asyncio.to_thread(self._exchange.cancel, symbol, oid)

    async def market_close(self, symbol: str) -> None:
        await asyncio.to_thread(self._exchange.market_close, symbol)

    # --- Other operations the live system needs --------------------------

    async def user_state(self) -> dict[str, Any]:
        state = await asyncio.to_thread(self._info.user_state, self._address)
        return state if isinstance(state, dict) else {}

    async def submit_alo(
        self,
        symbol: str,
        is_buy: bool,
        size: Decimal,
        price: Decimal,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Submit a post-only (ALO) limit order.

        Returns OrderResult with accepted=True and a resting oid on success,
        accepted=False with error_code='alo_would_take' on ALO rejection,
        accepted=False with the underlying error string for anything else.
        """
        order_type = {"limit": {"tif": "Alo"}}
        try:
            raw = await asyncio.to_thread(
                self._exchange.order,
                symbol,
                is_buy,
                float(size),
                float(price),
                order_type,
                reduce_only,
            )
        except Exception as exc:
            logger.exception("submit_alo raised")
            return OrderResult(accepted=False, oid=None, error_code=repr(exc), raw=None)

        if _is_alo_rejected(raw):
            return OrderResult(accepted=False, oid=None, error_code="alo_would_take", raw=raw)

        oid = _extract_oid(raw)
        if oid is None:
            return OrderResult(accepted=False, oid=None, error_code="no_oid", raw=raw)
        return OrderResult(accepted=True, oid=oid, error_code=None, raw=raw)
