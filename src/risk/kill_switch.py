"""Kill switch — the single API to fully halt trading.

Contract (RISK.md):
  halt(reason) MUST
    1. Set HALTED flag.
    2. Cancel all open orders on the exchange.
    3. Close all open positions with reduce-only market orders.
    4. Write halt reason and timestamp to the journal.
    5. Notify via Telegram.

  The HALTED flag is checked before every order intent. While HALTED,
  only resume() clears it.

The class depends on a thin ExchangeAdapter Protocol (not the SDK
directly) so it is unit-testable. The real adapter wraps Hyperliquid's
Info+Exchange clients on TESTNET.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from src.notify.telegram import Notifier
from src.storage.journal import JournalLike

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenOrderRef:
    symbol: str
    oid: int


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    size: Decimal  # signed: positive long, negative short


class ExchangeAdapter(Protocol):
    """Subset of the exchange API the kill switch needs.

    All methods are async and idempotent-safe to retry once.
    """

    async def list_open_orders(self) -> list[OpenOrderRef]: ...
    async def list_positions(self) -> list[PositionSnapshot]: ...
    async def cancel(self, symbol: str, oid: int) -> None: ...
    async def market_close(self, symbol: str) -> None: ...


class KillSwitch:
    """Holds the HALTED flag and runs the halt sequence."""

    def __init__(
        self,
        exchange: ExchangeAdapter,
        journal: JournalLike,
        notifier: Notifier,
    ) -> None:
        self._exchange = exchange
        self._journal = journal
        self._notifier = notifier
        self._halted = False
        self._lock = asyncio.Lock()

    @property
    def halted(self) -> bool:
        return self._halted

    async def halt(self, reason: str) -> None:
        """Run the full halt sequence. Idempotent: re-entry is a no-op."""
        async with self._lock:
            if self._halted:
                logger.info("halt() called while already halted: %s", reason)
                return
            self._halted = True

        ts = datetime.now(timezone.utc).isoformat()
        cancel_errors: list[str] = []
        close_errors: list[str] = []

        try:
            open_orders = await self._exchange.list_open_orders()
        except Exception as exc:
            open_orders = []
            cancel_errors.append(f"list_open_orders failed: {exc!r}")

        for ref in open_orders:
            try:
                await self._exchange.cancel(ref.symbol, ref.oid)
            except Exception as exc:
                cancel_errors.append(f"cancel({ref.symbol},{ref.oid}) failed: {exc!r}")

        try:
            positions = await self._exchange.list_positions()
        except Exception as exc:
            positions = []
            close_errors.append(f"list_positions failed: {exc!r}")

        closed_symbols: list[str] = []
        for pos in positions:
            if pos.size == 0:
                continue
            try:
                await self._exchange.market_close(pos.symbol)
                closed_symbols.append(pos.symbol)
            except Exception as exc:
                close_errors.append(f"market_close({pos.symbol}) failed: {exc!r}")

        await self._journal.append(
            "halt",
            {
                "reason": reason,
                "ts": ts,
                "cancelled_orders": [{"symbol": r.symbol, "oid": r.oid} for r in open_orders],
                "closed_positions": closed_symbols,
                "cancel_errors": cancel_errors,
                "close_errors": close_errors,
            },
        )

        try:
            await self._notifier.send(f"HALTED: {reason}")
        except Exception as exc:
            await self._journal.append(
                "notify_error",
                {"ts": ts, "where": "halt", "error": repr(exc)},
            )

    async def resume(self) -> None:
        """Clear the HALTED flag. Operator-only path."""
        async with self._lock:
            if not self._halted:
                return
            self._halted = False

        ts = datetime.now(timezone.utc).isoformat()
        await self._journal.append("resume", {"ts": ts})
        try:
            await self._notifier.send("Resumed")
        except Exception as exc:
            await self._journal.append(
                "notify_error",
                {"ts": ts, "where": "resume", "error": repr(exc)},
            )
