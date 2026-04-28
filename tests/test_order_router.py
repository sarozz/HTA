"""Tests for OrderRouter — ALO retry, stale cancel, halt gate, risk gate."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.exchange_adapter import OrderResult
from src.execution.order_router import OrderRouter
from src.execution.position_tracker import TrackedPosition
from src.risk.kill_switch import KillSwitch
from src.storage.journal import InMemoryJournal
from src.strategies.mean_reversion import TargetPosition


def _make_tracker(
    positions: list[TrackedPosition] | None = None, equity: Decimal = Decimal("1500")
):
    tracker = MagicMock()
    pos_map = {p.symbol: p for p in (positions or [])}
    tracker.get_position.side_effect = lambda s: pos_map.get(s)
    tracker.positions.return_value = list(pos_map.values())
    tracker.open_orders.return_value = []
    tracker.equity = equity
    tracker.free_margin = equity
    return tracker


def _make_kill_switch(halted: bool = False):
    ks = MagicMock(spec=KillSwitch)
    ks.halted = halted
    return ks


def _make_ticks(
    bid: Decimal = Decimal("99.99"),
    ask: Decimal = Decimal("100.01"),
    tick: Decimal = Decimal("0.01"),
):
    ts = MagicMock()
    ts.tick_size.return_value = tick
    ts.best_bid.return_value = bid
    ts.best_ask.return_value = ask
    return ts


def _make_adapter():
    a = MagicMock()
    a.submit_alo = AsyncMock()
    a.cancel = AsyncMock(return_value=None)
    return a


def _ok_result(oid: int = 12345) -> OrderResult:
    return OrderResult(accepted=True, oid=oid, error_code=None, raw={"resting": {"oid": oid}})


def _alo_reject() -> OrderResult:
    return OrderResult(
        accepted=False,
        oid=None,
        error_code="alo_would_take",
        raw={"error": "Order would immediately match (post only)"},
    )


def _other_failure() -> OrderResult:
    return OrderResult(accepted=False, oid=None, error_code="insufficient_margin", raw={})


# ---------------------------------------------------------------------------
# Halt gate
# ---------------------------------------------------------------------------


async def test_no_order_placed_when_halted() -> None:
    adapter = _make_adapter()
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(halted=True),
        tick_service=_make_ticks(),
        journal=journal,
    )
    target = TargetPosition(symbol="BTC", notional=Decimal("100"), reason="long")
    await router.submit_target(target)
    adapter.submit_alo.assert_not_called()
    assert any(e[1] == "order_blocked" for e in journal.events)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_first_alo_accepted_records_oid_and_journals() -> None:
    adapter = _make_adapter()
    adapter.submit_alo.return_value = _ok_result(oid=42)
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    adapter.submit_alo.assert_awaited_once()
    _args, kwargs = adapter.submit_alo.call_args
    assert kwargs["symbol"] == "BTC"
    assert kwargs["is_buy"] is True
    assert kwargs["price"] == Decimal("99.99")  # buyer posts at bid
    assert 42 in router.submitted_oids
    assert any(e[1] == "order_submitted" for e in journal.events)


# ---------------------------------------------------------------------------
# Reject-then-retry: the SPEC-mandated path
# ---------------------------------------------------------------------------


async def test_alo_rejected_retries_once_with_one_tick_improvement() -> None:
    adapter = _make_adapter()
    adapter.submit_alo.side_effect = [_alo_reject(), _ok_result(oid=99)]
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(bid=Decimal("99.99"), tick=Decimal("0.01")),
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    assert adapter.submit_alo.await_count == 2
    first_kwargs = adapter.submit_alo.call_args_list[0].kwargs
    second_kwargs = adapter.submit_alo.call_args_list[1].kwargs
    # Buyer first posts at bid (99.99); retry at bid - tick (99.98).
    assert first_kwargs["price"] == Decimal("99.99")
    assert second_kwargs["price"] == Decimal("99.98")
    assert 99 in router.submitted_oids
    assert any(e[1] == "order_submitted_after_retry" for e in journal.events)


async def test_alo_rejected_retries_for_sell_with_tick_above() -> None:
    adapter = _make_adapter()
    adapter.submit_alo.side_effect = [_alo_reject(), _ok_result(oid=7)]
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(ask=Decimal("100.01"), tick=Decimal("0.01")),
        journal=InMemoryJournal(),
    )
    # Existing long, target 0 → sell delta.
    tracker = _make_tracker(
        positions=[
            TrackedPosition(
                symbol="BTC",
                size=Decimal("0.1"),
                entry_price=Decimal("100"),
                mark_price=Decimal("100"),
                liq_price=None,
            )
        ]
    )
    router._tracker = tracker  # override
    await router.submit_target(TargetPosition("BTC", Decimal("0"), "exit"))
    assert adapter.submit_alo.await_count == 2
    first_kwargs = adapter.submit_alo.call_args_list[0].kwargs
    second_kwargs = adapter.submit_alo.call_args_list[1].kwargs
    assert first_kwargs["is_buy"] is False
    assert first_kwargs["price"] == Decimal("100.01")
    # Seller retries by posting one tick higher (ask + tick).
    assert second_kwargs["price"] == Decimal("100.02")


async def test_alo_rejected_twice_gives_up_and_journals() -> None:
    adapter = _make_adapter()
    adapter.submit_alo.side_effect = [_alo_reject(), _alo_reject()]
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    assert adapter.submit_alo.await_count == 2
    assert router.submitted_oids == []
    abandoned = [e for e in journal.events if e[1] == "order_abandoned"]
    assert len(abandoned) == 1
    assert abandoned[0][2]["first_error"] == "alo_would_take"
    assert abandoned[0][2]["retry_error"] == "alo_would_take"


async def test_non_alo_failure_does_not_retry() -> None:
    adapter = _make_adapter()
    adapter.submit_alo.return_value = _other_failure()
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    adapter.submit_alo.assert_awaited_once()  # no retry on non-ALO error
    assert any(e[1] == "order_failed" for e in journal.events)


# ---------------------------------------------------------------------------
# Stale order cancel
# ---------------------------------------------------------------------------


async def test_cancel_stale_orders_removes_orders_older_than_threshold() -> None:
    adapter = _make_adapter()
    fake_now = [1000.0]

    def clock() -> float:
        return fake_now[0]

    adapter.submit_alo.return_value = _ok_result(oid=11)
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=journal,
        stale_seconds=60.0,
        clock=clock,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    assert 11 in router.submitted_oids

    # Advance time past the threshold.
    fake_now[0] = 1100.0
    await router.cancel_stale_orders()
    adapter.cancel.assert_awaited_once_with("BTC", 11)
    assert router.submitted_oids == []
    assert any(e[1] == "order_cancelled_stale" for e in journal.events)


async def test_cancel_stale_leaves_fresh_orders_alone() -> None:
    adapter = _make_adapter()
    fake_now = [1000.0]
    adapter.submit_alo.return_value = _ok_result(oid=22)
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=InMemoryJournal(),
        stale_seconds=60.0,
        clock=lambda: fake_now[0],
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))

    fake_now[0] = 1030.0  # 30s later, still under 60s threshold
    await router.cancel_stale_orders()
    adapter.cancel.assert_not_called()
    assert 22 in router.submitted_oids


# ---------------------------------------------------------------------------
# No quote, zero delta, etc.
# ---------------------------------------------------------------------------


async def test_no_order_when_no_quote_available() -> None:
    adapter = _make_adapter()
    ticks = _make_ticks()
    ticks.best_bid.return_value = None
    ticks.best_ask.return_value = None
    journal = InMemoryJournal()
    router = OrderRouter(
        adapter=adapter,
        tracker=_make_tracker(),
        kill_switch=_make_kill_switch(),
        tick_service=ticks,
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    adapter.submit_alo.assert_not_called()
    assert any(e[1] == "order_skipped" for e in journal.events)


async def test_zero_delta_does_not_submit() -> None:
    adapter = _make_adapter()
    tracker = _make_tracker(
        positions=[
            TrackedPosition(
                symbol="BTC",
                size=Decimal("1"),
                entry_price=Decimal("100"),
                mark_price=Decimal("100"),
                liq_price=None,
            )
        ]
    )
    router = OrderRouter(
        adapter=adapter,
        tracker=tracker,
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(bid=Decimal("99.99"), ask=Decimal("100.01")),
        journal=InMemoryJournal(),
    )
    # Target 1.0 BTC at mid 100 → notional 100 → already there.
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "hold"))
    adapter.submit_alo.assert_not_called()


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------


async def test_risk_rejected_intent_is_not_sent() -> None:
    adapter = _make_adapter()
    journal = InMemoryJournal()
    # equity=0 forces risk manager to reject ("no_equity").
    tracker = _make_tracker(equity=Decimal("0"))
    router = OrderRouter(
        adapter=adapter,
        tracker=tracker,
        kill_switch=_make_kill_switch(),
        tick_service=_make_ticks(),
        journal=journal,
    )
    await router.submit_target(TargetPosition("BTC", Decimal("100"), "long"))
    adapter.submit_alo.assert_not_called()
    assert any(e[1] == "order_rejected_by_risk" for e in journal.events)


_ = pytest
