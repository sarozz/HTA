"""Tests for src/risk/manager.py.

For every cap in RISK.md there is a `block` test and an `allow` test.
The base state and intent are deliberately benign so any failure points
to the specific cap under test.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.risk.manager import (
    APPROVED,
    AccountState,
    Approved,
    OpenOrder,
    OrderIntent,
    Position,
    Rejected,
    RiskCaps,
    check_order,
)

NOW = 1_700_000_000.0


def _intent(**overrides: object) -> OrderIntent:
    base = dict(
        symbol="BTC",
        side="buy",
        size=Decimal("0.001"),
        price=Decimal("60000"),
        order_type="ALO",
        reduce_only=False,
        strategy="mean_reversion",
        risk_dollars=Decimal("3"),
    )
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _state(**overrides: object) -> AccountState:
    base = dict(
        equity=Decimal("1500"),
        free_margin=Decimal("1500"),
        positions=(),
        open_orders=(),
        recent_order_timestamps=(),
        daily_pnl=Decimal("0"),
        weekly_pnl=Decimal("0"),
        consecutive_losers=0,
        loss_pause_until=None,
        halted=False,
        now=NOW,
        caps=RiskCaps(),
    )
    base.update(overrides)
    return AccountState(**base)  # type: ignore[arg-type]


def _assert_rejected(decision: object, code: str) -> Rejected:
    assert isinstance(decision, Rejected), decision
    assert decision.code == code, decision
    return decision


# ---------------------------------------------------------------------------
# Sanity: baseline passes
# ---------------------------------------------------------------------------


def test_baseline_intent_is_approved() -> None:
    assert isinstance(check_order(_intent(), _state()), Approved)
    assert check_order(_intent(), _state()) == APPROVED


# ---------------------------------------------------------------------------
# Halt flag
# ---------------------------------------------------------------------------


def test_halted_blocks_order() -> None:
    _assert_rejected(check_order(_intent(), _state(halted=True)), "halted")


def test_not_halted_allows_order() -> None:
    assert isinstance(check_order(_intent(), _state(halted=False)), Approved)


# ---------------------------------------------------------------------------
# Per-trade caps
# ---------------------------------------------------------------------------


def test_max_risk_per_trade_blocks() -> None:
    # 0.5% of $1500 = $7.50; risking $7.51 fails.
    decision = check_order(_intent(risk_dollars=Decimal("7.51")), _state())
    _assert_rejected(decision, "max_risk_per_trade")


def test_max_risk_per_trade_allows_at_cap() -> None:
    decision = check_order(_intent(risk_dollars=Decimal("7.50")), _state())
    assert isinstance(decision, Approved)


def test_max_position_notional_blocks() -> None:
    # Cap is 30% of $1500 = $450. Notional = 0.01 * 60000 = $600.
    decision = check_order(_intent(size=Decimal("0.01")), _state())
    _assert_rejected(decision, "max_position_notional")


def test_max_position_notional_allows_under_cap() -> None:
    # Notional = 0.0075 * 60000 = $450 = exactly at cap.
    decision = check_order(_intent(size=Decimal("0.0075")), _state())
    assert isinstance(decision, Approved)


def test_max_account_leverage_blocks() -> None:
    # Existing BTC position: 4.5 @ $1000 mark = $4500 notional (3.0x leverage).
    # New order on ETH adds $300 notional → 4800/1500 = 3.2x.
    pos = Position(
        symbol="BTC",
        size=Decimal("4.5"),
        mark_price=Decimal("1000"),
        liq_price=None,
    )
    state = _state(positions=(pos,))
    intent = _intent(symbol="ETH", size=Decimal("1"), price=Decimal("300"))
    _assert_rejected(check_order(intent, state), "max_leverage")


def test_max_account_leverage_allows_under_cap() -> None:
    # Existing 1 ETH @ $1000 = $1000 notional; new BTC notional = $60 → 1.04x.
    pos = Position(
        symbol="ETH",
        size=Decimal("1"),
        mark_price=Decimal("1000"),
        liq_price=None,
    )
    state = _state(positions=(pos,))
    assert isinstance(check_order(_intent(), state), Approved)


def test_liquidation_buffer_blocks() -> None:
    # Existing BTC position with buffer = (100-95)/100 = 5% < 8% cap.
    # An open intent is rejected even on a different symbol.
    pos = Position(
        symbol="BTC",
        size=Decimal("0.01"),
        mark_price=Decimal("100"),
        liq_price=Decimal("95"),
    )
    state = _state(positions=(pos,))
    intent = _intent(symbol="ETH", size=Decimal("0.001"), price=Decimal("100"))
    _assert_rejected(check_order(intent, state), "liquidation_buffer")


def test_liquidation_buffer_allows_when_position_safe() -> None:
    pos = Position(
        symbol="BTC",
        size=Decimal("0.01"),
        mark_price=Decimal("100"),
        liq_price=Decimal("90"),  # 10% buffer
    )
    state = _state(positions=(pos,))
    intent = _intent(symbol="ETH", size=Decimal("0.001"), price=Decimal("100"))
    assert isinstance(check_order(intent, state), Approved)


def test_liquidation_buffer_skipped_for_reduce_only() -> None:
    # Same breached position, but a reduce-only order on it is what unwinds.
    pos = Position(
        symbol="BTC",
        size=Decimal("0.01"),
        mark_price=Decimal("100"),
        liq_price=Decimal("95"),
    )
    state = _state(positions=(pos,))
    intent = _intent(
        symbol="BTC",
        side="sell",
        size=Decimal("0.01"),
        price=Decimal("100"),
        reduce_only=True,
    )
    assert isinstance(check_order(intent, state), Approved)


# ---------------------------------------------------------------------------
# Concurrency caps
# ---------------------------------------------------------------------------


def test_max_concurrent_positions_blocks() -> None:
    # Already 2 positions; intent on a 3rd symbol would open another.
    p1 = Position("BTC", Decimal("0.001"), Decimal("60000"), None)
    p2 = Position("ETH", Decimal("0.01"), Decimal("3000"), None)
    state = _state(positions=(p1, p2))
    intent = _intent(symbol="SOL", size=Decimal("0.1"), price=Decimal("100"))
    _assert_rejected(check_order(intent, state), "max_concurrent_positions")


def test_max_concurrent_positions_allows_when_reducing() -> None:
    # Already 2 positions; reducing one to zero stays within the cap.
    p1 = Position("BTC", Decimal("0.001"), Decimal("60000"), None)
    p2 = Position("ETH", Decimal("0.01"), Decimal("3000"), None)
    state = _state(positions=(p1, p2))
    intent = _intent(
        symbol="ETH",
        side="sell",
        size=Decimal("0.01"),
        price=Decimal("3000"),
        reduce_only=True,
    )
    assert isinstance(check_order(intent, state), Approved)


def test_max_open_orders_blocks() -> None:
    six = tuple(OpenOrder("BTC", "buy", Decimal("0.001"), Decimal("60000")) for _ in range(6))
    _assert_rejected(check_order(_intent(), _state(open_orders=six)), "max_open_orders")


def test_max_open_orders_allows_under_cap() -> None:
    five = tuple(OpenOrder("BTC", "buy", Decimal("0.001"), Decimal("60000")) for _ in range(5))
    assert isinstance(check_order(_intent(), _state(open_orders=five)), Approved)


def test_max_orders_per_minute_blocks() -> None:
    timestamps = tuple(NOW - i for i in range(20))  # 20 within last 60s
    _assert_rejected(
        check_order(_intent(), _state(recent_order_timestamps=timestamps)),
        "rate_limit",
    )


def test_max_orders_per_minute_allows_under_cap() -> None:
    timestamps = tuple(NOW - i for i in range(19))
    assert isinstance(check_order(_intent(), _state(recent_order_timestamps=timestamps)), Approved)


def test_max_orders_per_minute_ignores_old_timestamps() -> None:
    # 30 stale + 5 fresh: only the 5 count.
    stale = tuple(NOW - 600 - i for i in range(30))
    fresh = tuple(NOW - i for i in range(5))
    assert isinstance(
        check_order(_intent(), _state(recent_order_timestamps=stale + fresh)),
        Approved,
    )


# ---------------------------------------------------------------------------
# Loss limits
# ---------------------------------------------------------------------------


def test_daily_loss_limit_blocks() -> None:
    # 2% of $1500 = $30; daily PnL = -$30.01 trips it.
    decision = check_order(_intent(), _state(daily_pnl=Decimal("-30.01")))
    _assert_rejected(decision, "daily_loss_limit")


def test_daily_loss_limit_allows_under_cap() -> None:
    decision = check_order(_intent(), _state(daily_pnl=Decimal("-29.99")))
    assert isinstance(decision, Approved)


def test_weekly_loss_limit_blocks() -> None:
    # 5% of $1500 = $75.
    decision = check_order(_intent(), _state(weekly_pnl=Decimal("-75.01")))
    _assert_rejected(decision, "weekly_loss_limit")


def test_weekly_loss_limit_allows_under_cap() -> None:
    decision = check_order(_intent(), _state(weekly_pnl=Decimal("-74.99")))
    assert isinstance(decision, Approved)


def test_consecutive_losers_pause_blocks_during_window() -> None:
    state = _state(consecutive_losers=3, loss_pause_until=NOW + 1)
    _assert_rejected(check_order(_intent(), state), "consecutive_losers_pause")


def test_consecutive_losers_pause_allows_after_window_expires() -> None:
    state = _state(consecutive_losers=3, loss_pause_until=NOW - 1)
    assert isinstance(check_order(_intent(), state), Approved)


# ---------------------------------------------------------------------------
# Misc validation
# ---------------------------------------------------------------------------


def test_zero_size_rejected() -> None:
    _assert_rejected(check_order(_intent(size=Decimal("0")), _state()), "bad_intent")


def test_zero_equity_rejected() -> None:
    _assert_rejected(check_order(_intent(), _state(equity=Decimal("0"))), "no_equity")


def test_caps_can_be_overridden_via_state() -> None:
    # Tighter cap via injected RiskCaps; same intent now blocks.
    tight = RiskCaps(max_risk_per_trade_pct=Decimal("0.001"))  # 0.1%
    decision = check_order(_intent(), replace(_state(), caps=tight))
    _assert_rejected(decision, "max_risk_per_trade")
