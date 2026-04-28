"""Translates TargetPosition deltas into ALO limit orders.

Responsibilities:
  - Compute the order size needed to bridge from current position to
    target notional.
  - Build OrderIntent and run it through the Risk Manager.
  - On approval, submit an ALO order at the touch.
  - If the ALO is rejected (would-take), retry once with one tick of
    price improvement, then give up.
  - Track submitted orders and cancel any that have rested longer than
    `stale_seconds` (default 60).
  - Honour the kill switch: while halted, no orders are placed.

Pre-conditions:
  - PositionTracker has been refreshed within the same loop tick so the
    "current position" we compute the delta against is exchange-truth.
  - TickService can quote a tick size for the symbol.

The exchange interaction is delegated to HyperliquidExchangeAdapter so
this class is testable without the SDK.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from src.execution.exchange_adapter import HyperliquidExchangeAdapter, OrderResult
from src.execution.position_tracker import PositionTracker, TrackedPosition
from src.risk.kill_switch import KillSwitch
from src.risk.manager import (
    AccountState,
    Approved,
    OpenOrder,
    OrderIntent,
    Position,
    Rejected,
    check_order,
)
from src.storage.journal import JournalLike
from src.strategies.mean_reversion import TargetPosition

logger = logging.getLogger(__name__)


class TickService(Protocol):
    def tick_size(self, symbol: str) -> Decimal: ...
    def best_bid(self, symbol: str) -> Decimal | None: ...
    def best_ask(self, symbol: str) -> Decimal | None: ...


@dataclass
class _SubmittedOrder:
    oid: int
    symbol: str
    is_buy: bool
    size: Decimal
    price: Decimal
    submitted_at_monotonic: float


class OrderRouter:
    def __init__(
        self,
        adapter: HyperliquidExchangeAdapter,
        tracker: PositionTracker,
        kill_switch: KillSwitch,
        tick_service: TickService,
        journal: JournalLike,
        *,
        stale_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._tracker = tracker
        self._kill_switch = kill_switch
        self._ticks = tick_service
        self._journal = journal
        self._stale_seconds = stale_seconds
        self._clock = clock
        self._submitted: dict[int, _SubmittedOrder] = {}
        self._recent_submissions: list[float] = []

    @property
    def submitted_oids(self) -> list[int]:
        return list(self._submitted.keys())

    async def submit_target(
        self,
        target: TargetPosition,
        strategy: str = "mean_reversion",
    ) -> None:
        """Bridge from the tracker's current position to `target.notional`.

        No-ops if the kill switch is set, if the delta is zero, or if the
        Risk Manager rejects the resulting intent.
        """
        if self._kill_switch.halted:
            await self._journal.append(
                "order_blocked",
                {"symbol": target.symbol, "reason": "halted"},
            )
            return

        current = self._tracker.get_position(target.symbol)
        delta = await self._compute_delta(target, current)
        if delta is None:
            return

        side, size_abs, price, reduce_only = delta

        intent = OrderIntent(
            symbol=target.symbol,
            side=side,
            size=size_abs,
            price=price,
            order_type="ALO",
            reduce_only=reduce_only,
            strategy=strategy,
            risk_dollars=self._estimated_risk_dollars(target.symbol, size_abs, price),
        )

        state = self._build_account_state()
        decision = check_order(intent, state)
        if isinstance(decision, Rejected):
            await self._journal.append(
                "order_rejected_by_risk",
                {
                    "symbol": intent.symbol,
                    "code": decision.code,
                    "reason": decision.reason,
                    "intent": _intent_to_dict(intent),
                },
            )
            return
        assert isinstance(decision, Approved)

        await self._send_alo_with_retry(intent)

    async def cancel_stale_orders(self) -> None:
        now = self._clock()
        for oid, order in list(self._submitted.items()):
            if now - order.submitted_at_monotonic <= self._stale_seconds:
                continue
            try:
                await self._adapter.cancel(order.symbol, oid)
            except Exception as exc:
                logger.warning("cancel(%s, %d) failed: %r", order.symbol, oid, exc)
                await self._journal.append(
                    "cancel_failed",
                    {"symbol": order.symbol, "oid": oid, "error": repr(exc)},
                )
                continue
            await self._journal.append(
                "order_cancelled_stale",
                {
                    "symbol": order.symbol,
                    "oid": oid,
                    "age_seconds": now - order.submitted_at_monotonic,
                },
            )
            self._submitted.pop(oid, None)

    # ----- internals -------------------------------------------------------

    async def _compute_delta(
        self,
        target: TargetPosition,
        current: TrackedPosition | None,
    ) -> tuple[str, Decimal, Decimal, bool] | None:
        """Return (side, abs_size, price, reduce_only) or None if no order needed."""
        bid = self._ticks.best_bid(target.symbol)
        ask = self._ticks.best_ask(target.symbol)
        if bid is None or ask is None:
            await self._journal.append(
                "order_skipped",
                {"symbol": target.symbol, "reason": "no quote"},
            )
            return None

        current_size = current.size if current is not None else Decimal("0")
        # Use mid for converting target notional to size (reference price).
        ref_price = (bid + ask) / Decimal("2")
        target_size = target.notional / ref_price if ref_price > 0 else Decimal("0")
        delta = target_size - current_size

        # Quantise size to a sane number of decimals (Hyperliquid pulls
        # szDecimals from meta; we don't want to pretend we know more
        # precision than the exchange will accept). Eight decimals is fine
        # as a generic upper bound — meta-aware quantisation lives in the
        # tick service when integrated.
        delta = delta.quantize(Decimal("0.00000001"))
        if delta == 0:
            return None

        is_buy = delta > 0
        side = "buy" if is_buy else "sell"
        size_abs = abs(delta)

        # ALO at the touch: buyer posts at bid, seller posts at ask.
        price = bid if is_buy else ask

        # reduce_only iff the order strictly reduces |position| and doesn't flip.
        reduce_only = (
            current_size != 0
            and (
                (current_size > 0 and not is_buy and abs(target_size) < abs(current_size))
                or (current_size < 0 and is_buy and abs(target_size) < abs(current_size))
            )
            and (target_size * current_size) >= 0  # no sign flip
        )

        return side, size_abs, price, reduce_only

    def _estimated_risk_dollars(self, symbol: str, size: Decimal, price: Decimal) -> Decimal:
        """Conservative risk estimate for the Risk Manager.

        The strategy already enforces a hard 0.5%-of-equity cap via its
        sizing formula. Re-derive that here as the worst case so the risk
        check is independent of the strategy's own arithmetic.
        """
        notional = size * price
        # A z=2.0 entry to z=3.5 stop is roughly 1.5 sigma; a typical 5m
        # sigma on majors is ~10-30 bps. Using 30 bps as a defensive upper
        # bound keeps the estimate honest without strategy-specific data.
        worst_case_pct = Decimal("0.003")
        return notional * worst_case_pct

    def _build_account_state(self) -> AccountState:
        positions = tuple(
            Position(
                symbol=p.symbol,
                size=p.size,
                mark_price=p.mark_price,
                liq_price=p.liq_price,
            )
            for p in self._tracker.positions()
        )
        open_orders = tuple(
            OpenOrder(symbol=o.symbol, side="buy", size=o.size, price=o.price)
            for o in self._tracker.open_orders()
        )
        now = time.time()
        # Trim recent timestamps to the last 60s.
        cutoff = now - 60.0
        self._recent_submissions = [t for t in self._recent_submissions if t >= cutoff]
        return AccountState(
            equity=self._tracker.equity if self._tracker.equity > 0 else Decimal("1"),
            free_margin=self._tracker.free_margin,
            positions=positions,
            open_orders=open_orders,
            recent_order_timestamps=tuple(self._recent_submissions),
            halted=self._kill_switch.halted,
            now=now,
        )

    async def _send_alo_with_retry(self, intent: OrderIntent) -> None:
        # First attempt at the touch.
        result = await self._adapter.submit_alo(
            symbol=intent.symbol,
            is_buy=(intent.side == "buy"),
            size=intent.size,
            price=intent.price,
            reduce_only=intent.reduce_only,
        )
        if result.accepted and result.oid is not None:
            self._record_submission(intent, result)
            await self._journal.append("order_submitted", _result_to_dict(intent, result))
            return

        if result.error_code != "alo_would_take":
            await self._journal.append(
                "order_failed",
                {"symbol": intent.symbol, "error": result.error_code, "raw": str(result.raw)[:500]},
            )
            return

        # Retry once with one tick of price improvement.
        tick = self._ticks.tick_size(intent.symbol)
        improved_price = intent.price - tick if intent.side == "buy" else intent.price + tick
        retry_intent = OrderIntent(
            symbol=intent.symbol,
            side=intent.side,
            size=intent.size,
            price=improved_price,
            order_type=intent.order_type,
            reduce_only=intent.reduce_only,
            strategy=intent.strategy,
            risk_dollars=intent.risk_dollars,
        )
        retry = await self._adapter.submit_alo(
            symbol=retry_intent.symbol,
            is_buy=(retry_intent.side == "buy"),
            size=retry_intent.size,
            price=retry_intent.price,
            reduce_only=retry_intent.reduce_only,
        )
        if retry.accepted and retry.oid is not None:
            self._record_submission(retry_intent, retry)
            await self._journal.append(
                "order_submitted_after_retry",
                _result_to_dict(retry_intent, retry),
            )
            return

        await self._journal.append(
            "order_abandoned",
            {
                "symbol": intent.symbol,
                "first_error": result.error_code,
                "retry_error": retry.error_code,
            },
        )

    def _record_submission(self, intent: OrderIntent, result: OrderResult) -> None:
        assert result.oid is not None
        self._submitted[result.oid] = _SubmittedOrder(
            oid=result.oid,
            symbol=intent.symbol,
            is_buy=(intent.side == "buy"),
            size=intent.size,
            price=intent.price,
            submitted_at_monotonic=self._clock(),
        )
        self._recent_submissions.append(time.time())


def _intent_to_dict(intent: OrderIntent) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "side": intent.side,
        "size": str(intent.size),
        "price": str(intent.price),
        "type": intent.order_type,
        "reduce_only": intent.reduce_only,
        "strategy": intent.strategy,
        "risk_dollars": str(intent.risk_dollars),
    }


def _result_to_dict(intent: OrderIntent, result: OrderResult) -> dict[str, Any]:
    d = _intent_to_dict(intent)
    d["oid"] = result.oid
    return d


# Make asyncio importable in case someone relies on it (router doesn't use directly).
_ = asyncio
