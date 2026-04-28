"""Multi-strategy integration tests.

Both strategies want to open at the same time. Verifies:
  - The capital allocator splits equity per the configured shares
  - Neither strategy is starved unfairly when both are at their nominal max
  - Strategy B unwinds when Strategy A's position eats into B's budget
  - The Risk Manager's max-2-concurrent-positions cap is enforced across
    strategies (since both submit through the same check_order)
  - Halt blocks both strategies' submissions equally

The exchange / SDK is fully mocked; nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.capital_allocator import allocate
from src.execution.exchange_adapter import OrderResult
from src.execution.order_router import OrderRouter
from src.notify.telegram import TelegramNotifier
from src.risk.kill_switch import KillSwitch
from src.risk.manager import (
    AccountState,
    OrderIntent,
    Position,
    RiskCaps,
    check_order,
)
from src.storage.journal import InMemoryJournal
from src.strategies.funding_capture import (
    AccountSnapshot,
    FundingCaptureSymbolState,
    FundingRateInfo,
)
from src.strategies.funding_capture import (
    evaluate as evaluate_funding,
)
from src.strategies.mean_reversion import TargetPosition


def _empty_user_state() -> dict:
    return {
        "marginSummary": {"accountValue": "1500.00", "totalRawUsd": "1500.00"},
        "assetPositions": [],
    }


def _make_adapter() -> MagicMock:
    a = MagicMock()
    a.user_state = AsyncMock(return_value=_empty_user_state())
    a.list_open_orders = AsyncMock(return_value=[])
    a.list_positions = AsyncMock(return_value=[])
    a.cancel = AsyncMock(return_value=None)
    a.market_close = AsyncMock(return_value=None)
    a.submit_alo = AsyncMock(
        return_value=OrderResult(accepted=True, oid=1, error_code=None, raw={})
    )
    return a


def _make_tracker(
    positions: list[tuple[str, Decimal, Decimal]] | None = None,
    equity: Decimal = Decimal("1500"),
) -> MagicMock:
    """positions: list of (symbol, size, mark_price)"""
    tracker = MagicMock()
    pos_map = {}
    if positions:
        for sym, size, mark in positions:
            pos_map[sym] = MagicMock(
                symbol=sym,
                size=size,
                entry_price=mark,
                mark_price=mark,
                liq_price=None,
            )
    tracker.get_position.side_effect = lambda s: pos_map.get(s)
    tracker.positions.return_value = list(pos_map.values())
    tracker.open_orders.return_value = []
    tracker.equity = equity
    tracker.free_margin = equity
    return tracker


def _make_ticks(bid: Decimal = Decimal("99.99"), ask: Decimal = Decimal("100.01")):
    ts = MagicMock()
    ts.tick_size.return_value = Decimal("0.01")
    ts.best_bid.return_value = bid
    ts.best_ask.return_value = ask
    return ts


# ---------------------------------------------------------------------------
# Allocator + scheduler integration
# ---------------------------------------------------------------------------


def test_both_strategies_get_their_full_share_when_neither_is_using_capital() -> None:
    equity = Decimal("1500")
    a = allocate(equity, {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")})
    assert a["mean_reversion"].available_notional == Decimal("375")
    assert a["funding_capture"].available_notional == Decimal("750")
    # Buffer is preserved.
    assert (
        a["mean_reversion"].available_notional
        + a["funding_capture"].available_notional
        + equity * Decimal("0.25")
    ) == equity


def test_b_scheduler_sizes_to_allocator_budget_when_a_is_idle() -> None:
    """A using zero capital → B asks for and gets up to 50% of equity."""
    equity = Decimal("1500")
    allocations = allocate(
        equity, {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")}
    )
    b_alloc = allocations["funding_capture"]
    now = datetime(2026, 4, 1, 11, 50, tzinfo=timezone.utc)
    rate = FundingRateInfo(
        coin="BTC",
        predicted_rate=Decimal("0.002"),
        next_tick_time=now + timedelta(minutes=10),
        as_of=now,
    )
    decisions = evaluate_funding(
        now=now,
        funding_rates={"BTC": rate},
        account=AccountSnapshot(
            equity=equity, available_notional_for_strategy=b_alloc.available_notional
        ),
        states={"BTC": FundingCaptureSymbolState(coin="BTC")},
    )
    btc = decisions[0]
    assert btc.perp_target is not None
    assert abs(btc.perp_target.notional) == Decimal("750")  # 50%


def test_b_size_degrades_when_a_consumes_extra_budget() -> None:
    """A using $750 (its nominal × 2) → B's available shrinks to preserve buffer."""
    equity = Decimal("1500")
    allocations = allocate(
        equity, {"mean_reversion": Decimal("750"), "funding_capture": Decimal("0")}
    )
    b_alloc = allocations["funding_capture"]
    assert b_alloc.degraded
    assert b_alloc.available_notional == Decimal("375")  # equity - buffer - A_used

    now = datetime(2026, 4, 1, 11, 50, tzinfo=timezone.utc)
    rate = FundingRateInfo(
        coin="BTC",
        predicted_rate=Decimal("0.002"),
        next_tick_time=now + timedelta(minutes=10),
        as_of=now,
    )
    decisions = evaluate_funding(
        now=now,
        funding_rates={"BTC": rate},
        account=AccountSnapshot(
            equity=equity, available_notional_for_strategy=b_alloc.available_notional
        ),
        states={"BTC": FundingCaptureSymbolState(coin="BTC")},
    )
    # B sized to the smaller available, not its nominal $750.
    assert abs(decisions[0].perp_target.notional) == Decimal("375")


def test_b_unwinds_in_holding_when_a_position_grows_into_b_budget() -> None:
    """User-stated requirement: B unwinds correctly if A's position grows.

    Scenario: B is mid-capture (HOLDING). Mark price moves and A's position
    notional now consumes more capital, leaving B's allocator budget below
    half of what B is holding. The scheduler emits an early-exit pair.
    """
    equity = Decimal("1500")
    # A initially sized at $400 (just over its $375 share); equity expanded
    # so A's mark notional now $1100 — way over.
    allocations = allocate(
        equity,
        {"mean_reversion": Decimal("1100"), "funding_capture": Decimal("750")},
    )
    b_alloc = allocations["funding_capture"]
    # Available is clipped to whatever is left after buffer + A: 1500 - 375 - 1100 - 750 = -725 → 0.
    assert b_alloc.available_notional == Decimal("0")

    tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = tick - timedelta(seconds=30)
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="holding",
        target_tick_time=tick,
        target_notional=Decimal("-750"),
        perp_size=Decimal("-750"),
        spot_size=Decimal("750"),
    )
    decisions = evaluate_funding(
        now=now,
        funding_rates={
            "BTC": FundingRateInfo(
                coin="BTC",
                predicted_rate=Decimal("0.002"),
                next_tick_time=tick,
                as_of=now,
            )
        },
        account=AccountSnapshot(
            equity=equity, available_notional_for_strategy=b_alloc.available_notional
        ),
        states={"BTC": state},
    )
    btc = decisions[0]
    assert btc.next_state.phase == "exit_pending"
    assert btc.perp_target.notional == Decimal("0")
    assert btc.spot_target.notional == Decimal("0")
    assert "capital starvation" in btc.reason


# ---------------------------------------------------------------------------
# Risk Manager — max-2-concurrent-positions cap across strategies
# ---------------------------------------------------------------------------


def test_risk_manager_blocks_third_concurrent_position() -> None:
    """A and B each hold a position; a third strategy intent on a NEW
    symbol must be rejected. Both A and B intents go through the same
    check_order, so the cap is enforced uniformly."""
    state = AccountState(
        equity=Decimal("1500"),
        free_margin=Decimal("1500"),
        positions=(
            Position("BTC", Decimal("0.001"), Decimal("60000"), None),
            Position("ETH", Decimal("0.01"), Decimal("3000"), None),
        ),
        open_orders=(),
        recent_order_timestamps=(),
        halted=False,
        now=1_700_000_000.0,
        caps=RiskCaps(),
    )
    intent = OrderIntent(
        symbol="SOL",
        side="buy",
        size=Decimal("0.1"),
        price=Decimal("100"),
        order_type="ALO",
        reduce_only=False,
        strategy="funding_capture",  # different strategy, doesn't matter
        risk_dollars=Decimal("3"),
    )
    decision = check_order(intent, state)
    from src.risk.manager import Rejected

    assert isinstance(decision, Rejected)
    assert decision.code == "max_concurrent_positions"


def test_risk_manager_caps_apply_uniformly_regardless_of_strategy_field() -> None:
    """Strategy A and Strategy B intents at the same risk level both pass
    or both fail — the strategy tag is journal data, not a risk override."""
    state = AccountState(
        equity=Decimal("1500"),
        free_margin=Decimal("1500"),
        positions=(),
        open_orders=(),
        recent_order_timestamps=(),
        halted=False,
        now=1_700_000_000.0,
        caps=RiskCaps(),
    )
    benign_a = OrderIntent(
        symbol="BTC",
        side="buy",
        size=Decimal("0.001"),
        price=Decimal("60000"),
        order_type="ALO",
        reduce_only=False,
        strategy="mean_reversion",
        risk_dollars=Decimal("3"),
    )
    benign_b = OrderIntent(
        symbol="BTC",
        side="buy",
        size=Decimal("0.001"),
        price=Decimal("60000"),
        order_type="ALO",
        reduce_only=False,
        strategy="funding_capture",
        risk_dollars=Decimal("3"),
    )
    from src.risk.manager import Approved

    assert isinstance(check_order(benign_a, state), Approved)
    assert isinstance(check_order(benign_b, state), Approved)


# ---------------------------------------------------------------------------
# Halt blocks both strategies
# ---------------------------------------------------------------------------


async def test_halt_blocks_both_strategies_through_router() -> None:
    """OrderRouter.submit_target with kill_switch.halted=True blocks
    submissions regardless of which strategy submitted."""
    adapter = _make_adapter()
    journal = InMemoryJournal()
    notifier = TelegramNotifier(journal)
    kill = KillSwitch(adapter, journal, notifier)
    tracker = _make_tracker()
    router = OrderRouter(
        adapter=adapter,
        tracker=tracker,
        kill_switch=kill,
        tick_service=_make_ticks(),
        journal=journal,
    )
    await kill.halt("test")

    await router.submit_target(
        TargetPosition("BTC", Decimal("100"), "long"), strategy="mean_reversion"
    )
    await router.submit_target(
        TargetPosition("ETH", Decimal("100"), "short"), strategy="funding_capture"
    )
    adapter.submit_alo.assert_not_called()
    blocks = [e for e in journal.events if e[1] == "order_blocked"]
    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# Fairness — neither starves the other when both want to enter
# ---------------------------------------------------------------------------


def test_simultaneous_entry_neither_starves_the_other() -> None:
    """At t=T-10m for both strategies, with zero current usage, both get
    to enter at their nominal share. A: $375, B: $750."""
    equity = Decimal("1500")
    a = allocate(equity, {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")})
    assert a["mean_reversion"].available_notional > 0
    assert a["funding_capture"].available_notional > 0
    # Their combined share + buffer == equity. No starvation.
    assert (
        a["mean_reversion"].available_notional + a["funding_capture"].available_notional
    ) == equity * Decimal("0.75")


def test_round_trip_b_completes_then_a_can_use_full_share() -> None:
    """B opens, holds, exits, returning equity to the pool; A can then
    use its full nominal share."""
    equity = Decimal("1500")
    # Phase 1: B opens with full $750.
    a1 = allocate(equity, {"mean_reversion": Decimal("0"), "funding_capture": Decimal("750")})
    assert a1["mean_reversion"].available_notional == Decimal("375")
    # Phase 2: B exits, freeing up its capital.
    a2 = allocate(equity, {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")})
    assert a2["mean_reversion"].available_notional == Decimal("375")
    assert a2["funding_capture"].available_notional == Decimal("750")


# Fairness across strategy ordering: regardless of which strategy is
# evaluated first per loop tick, the allocator returns the same numbers.
def test_allocator_is_order_independent() -> None:
    equity = Decimal("1500")
    a1 = allocate(
        equity,
        {"mean_reversion": Decimal("200"), "funding_capture": Decimal("400")},
    )
    a2 = allocate(
        equity,
        {"funding_capture": Decimal("400"), "mean_reversion": Decimal("200")},
    )
    assert a1["mean_reversion"].available_notional == a2["mean_reversion"].available_notional
    assert a1["funding_capture"].available_notional == a2["funding_capture"].available_notional


_ = pytest
