"""Risk Manager — pure gatekeeper for order intents.

`check_order` is the single function every order must pass before reaching
the Order Router. It is pure: no I/O, no clocks, no globals. Anything the
decision depends on must be present in `AccountState`.

Caps enforced (see RISK.md):
  Per-trade
    - max_risk_per_trade_pct
    - max_position_notional_pct
    - max_account_leverage
    - liquidation_buffer_pct
  Concurrency
    - max_concurrent_positions
    - max_open_orders
    - max_orders_per_minute
  Loss limits
    - daily_loss_limit_pct
    - weekly_loss_limit_pct
    - consecutive_losers_pause
  Plus
    - HALTED flag
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Side = Literal["buy", "sell"]
OrderType = Literal["ALO", "GTC", "IOC", "MARKET"]


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Approved:
    pass


@dataclass(frozen=True)
class Rejected:
    code: str
    reason: str


Decision = Approved | Rejected
APPROVED: Approved = Approved()


def _reject(code: str, reason: str) -> Rejected:
    return Rejected(code=code, reason=reason)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    size: Decimal
    price: Decimal
    order_type: OrderType
    reduce_only: bool
    strategy: str
    risk_dollars: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    size: Decimal  # signed: positive long, negative short
    mark_price: Decimal
    liq_price: Decimal | None
    strategy: str | None = None


@dataclass(frozen=True)
class OpenOrder:
    symbol: str
    side: Side
    size: Decimal
    price: Decimal
    strategy: str | None = None


@dataclass(frozen=True)
class RiskCaps:
    max_risk_per_trade_pct: Decimal = Decimal("0.005")
    max_position_notional_pct: Decimal = Decimal("0.30")
    max_account_leverage: Decimal = Decimal("3.0")
    liquidation_buffer_pct: Decimal = Decimal("0.08")
    max_concurrent_positions: int = 2
    max_open_orders: int = 6
    max_orders_per_minute: int = 20
    daily_loss_limit_pct: Decimal = Decimal("0.02")
    weekly_loss_limit_pct: Decimal = Decimal("0.05")
    consecutive_losers_pause: int = 3
    consecutive_losers_pause_seconds: int = 60 * 60


@dataclass(frozen=True)
class AccountState:
    equity: Decimal
    free_margin: Decimal
    positions: tuple[Position, ...] = ()
    open_orders: tuple[OpenOrder, ...] = ()
    # Epoch-second timestamps of orders submitted in the recent past. The
    # caller is responsible for trimming this list; the manager only counts
    # entries within the last 60s of `now`.
    recent_order_timestamps: tuple[float, ...] = ()
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    consecutive_losers: int = 0
    # Epoch second until which trading is paused after consecutive losers.
    loss_pause_until: float | None = None
    halted: bool = False
    now: float = 0.0  # epoch seconds; injected for purity
    caps: RiskCaps = field(default_factory=RiskCaps)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signed_size_delta(intent: OrderIntent) -> Decimal:
    """How the order changes signed position size assuming a full fill."""
    delta = intent.size if intent.side == "buy" else -intent.size
    return delta


def _position_for(state: AccountState, symbol: str) -> Position | None:
    for p in state.positions:
        if p.symbol == symbol:
            return p
    return None


def _post_fill_size(intent: OrderIntent, current: Decimal) -> Decimal:
    """Resulting signed size after the intent fills, respecting reduce_only."""
    delta = _signed_size_delta(intent)
    if not intent.reduce_only:
        return current + delta
    # reduce_only: order can only shrink magnitude, never flip sign or grow.
    if current == 0:
        return Decimal("0")
    new = current + delta
    if (current > 0 and new < 0) or (current < 0 and new > 0):
        return Decimal("0")
    if abs(new) > abs(current):
        return current
    return new


def _account_notional(state: AccountState) -> Decimal:
    return sum((abs(p.size) * p.mark_price for p in state.positions), Decimal("0"))


def _count_within_last_minute(timestamps: tuple[float, ...], now: float) -> int:
    cutoff = now - 60.0
    return sum(1 for t in timestamps if t >= cutoff)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_order(intent: OrderIntent, state: AccountState) -> Decision:
    """Return Approved or Rejected(code, reason). Pure function."""
    caps = state.caps

    # 0. Halt flag (cheapest, hardest stop).
    if state.halted:
        return _reject("halted", "Risk Manager halted; orders rejected.")

    if state.equity <= 0:
        return _reject("no_equity", "Account equity is zero or negative.")

    # 1. Loss limits — refuse to keep digging once limits are hit.
    daily_limit_dollars = caps.daily_loss_limit_pct * state.equity
    if -state.daily_pnl >= daily_limit_dollars:
        return _reject(
            "daily_loss_limit",
            f"Daily loss {state.daily_pnl} <= -{daily_limit_dollars}.",
        )

    weekly_limit_dollars = caps.weekly_loss_limit_pct * state.equity
    if -state.weekly_pnl >= weekly_limit_dollars:
        return _reject(
            "weekly_loss_limit",
            f"Weekly loss {state.weekly_pnl} <= -{weekly_limit_dollars}.",
        )

    if (
        state.consecutive_losers >= caps.consecutive_losers_pause
        and state.loss_pause_until is not None
        and state.now < state.loss_pause_until
    ):
        return _reject(
            "consecutive_losers_pause",
            f"{state.consecutive_losers} losers in a row; paused until {state.loss_pause_until}.",
        )

    # 2. Concurrency caps. Reduce-only orders don't open new positions and
    #    don't add to open-order count beyond what's already happening — but
    #    we still count them toward open-order and rate-limit caps.
    recent = _count_within_last_minute(state.recent_order_timestamps, state.now)
    if recent >= caps.max_orders_per_minute:
        return _reject(
            "rate_limit",
            f"{recent} orders in last 60s >= cap {caps.max_orders_per_minute}.",
        )

    if len(state.open_orders) >= caps.max_open_orders:
        return _reject(
            "max_open_orders",
            f"{len(state.open_orders)} open orders >= cap {caps.max_open_orders}.",
        )

    # 3. Per-trade caps.
    if intent.size <= 0 or intent.price <= 0:
        return _reject("bad_intent", "Order size and price must be positive.")

    if not intent.reduce_only:
        if intent.risk_dollars > caps.max_risk_per_trade_pct * state.equity:
            return _reject(
                "max_risk_per_trade",
                f"Risk ${intent.risk_dollars} > "
                f"{caps.max_risk_per_trade_pct * 100}% of equity ${state.equity}.",
            )

    current_pos = _position_for(state, intent.symbol)
    current_size = current_pos.size if current_pos else Decimal("0")
    new_size = _post_fill_size(intent, current_size)
    new_notional = abs(new_size) * intent.price
    notional_cap = caps.max_position_notional_pct * state.equity
    if new_notional > notional_cap:
        return _reject(
            "max_position_notional",
            f"Post-fill notional ${new_notional} > cap ${notional_cap} "
            f"({caps.max_position_notional_pct * 100}% of equity).",
        )

    # Concurrent positions cap: count distinct symbols with non-zero size
    # post-fill. Reducing/closing an existing position never violates this.
    post_fill_symbols: set[str] = set()
    for p in state.positions:
        if p.symbol == intent.symbol:
            if new_size != 0:
                post_fill_symbols.add(p.symbol)
        elif p.size != 0:
            post_fill_symbols.add(p.symbol)
    if current_pos is None and new_size != 0:
        post_fill_symbols.add(intent.symbol)
    if len(post_fill_symbols) > caps.max_concurrent_positions:
        return _reject(
            "max_concurrent_positions",
            f"Post-fill positions {len(post_fill_symbols)} > "
            f"cap {caps.max_concurrent_positions}.",
        )

    # Account leverage post-fill = sum |notional| / equity.
    post_fill_notional = Decimal("0")
    for p in state.positions:
        if p.symbol == intent.symbol:
            post_fill_notional += abs(new_size) * intent.price
        else:
            post_fill_notional += abs(p.size) * p.mark_price
    if current_pos is None:
        post_fill_notional += abs(new_size) * intent.price
    leverage = post_fill_notional / state.equity
    if leverage > caps.max_account_leverage:
        return _reject(
            "max_leverage",
            f"Post-fill account leverage {leverage} > cap {caps.max_account_leverage}.",
        )

    # Liquidation buffer: every existing position must be >=8% from liq price.
    # Reduce-only orders only move us further from liq, so we let them through
    # even if a position is already breached — that's what unwinds it.
    if not intent.reduce_only:
        for p in state.positions:
            if p.liq_price is None or p.size == 0 or p.mark_price == 0:
                continue
            buffer = abs(p.mark_price - p.liq_price) / p.mark_price
            if buffer < caps.liquidation_buffer_pct:
                return _reject(
                    "liquidation_buffer",
                    f"{p.symbol} liq buffer {buffer} < cap {caps.liquidation_buffer_pct}.",
                )

    return APPROVED
