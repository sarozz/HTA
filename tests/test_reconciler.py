"""Tests for Reconciler — halts on any divergence between cache and exchange."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.position_tracker import TrackedOrder, TrackedPosition
from src.execution.reconciler import Reconciler
from src.risk.kill_switch import KillSwitch
from src.storage.journal import InMemoryJournal


def _tracker(
    positions, orders, refresh_to_positions=None, refresh_to_orders=None, refresh_raises=None
):
    """Build a fake tracker. After refresh(), positions/orders flip to refresh_to_*."""
    state = {
        "positions": list(positions),
        "orders": list(orders),
    }
    refresh_target = {
        "positions": list(refresh_to_positions if refresh_to_positions is not None else positions),
        "orders": list(refresh_to_orders if refresh_to_orders is not None else orders),
    }
    t = MagicMock()
    t.positions.side_effect = lambda: list(state["positions"])
    t.open_orders.side_effect = lambda: list(state["orders"])

    async def refresh() -> None:
        if refresh_raises:
            raise refresh_raises
        state["positions"] = list(refresh_target["positions"])
        state["orders"] = list(refresh_target["orders"])

    t.refresh = AsyncMock(side_effect=refresh)
    return t


def _make_kill_switch(halted: bool = False) -> KillSwitch:
    ks = MagicMock(spec=KillSwitch)
    ks.halted = halted
    ks.halt = AsyncMock()
    return ks


def _pos(symbol: str, size: Decimal) -> TrackedPosition:
    return TrackedPosition(
        symbol=symbol,
        size=size,
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        liq_price=None,
    )


def _order(oid: int, symbol: str = "BTC") -> TrackedOrder:
    return TrackedOrder(
        symbol=symbol,
        oid=oid,
        is_buy=True,
        size=Decimal("0.01"),
        price=Decimal("100"),
    )


# ---------------------------------------------------------------------------
# Convergent state — no halt
# ---------------------------------------------------------------------------


async def test_no_halt_when_cache_matches_exchange() -> None:
    tracker = _tracker(
        positions=[_pos("BTC", Decimal("0.1"))],
        orders=[_order(1)],
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_not_called()


async def test_no_halt_when_already_halted() -> None:
    tracker = _tracker(
        positions=[_pos("BTC", Decimal("0.1"))],
        orders=[],
        refresh_to_positions=[_pos("BTC", Decimal("0.5"))],  # divergent, but halted
    )
    ks = _make_kill_switch(halted=True)
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_not_called()  # already halted; no recursion


# ---------------------------------------------------------------------------
# Divergent state — must halt
# ---------------------------------------------------------------------------


async def test_halts_when_cached_position_differs_from_exchange() -> None:
    tracker = _tracker(
        positions=[_pos("BTC", Decimal("0.1"))],
        orders=[],
        refresh_to_positions=[_pos("BTC", Decimal("0.5"))],
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_awaited_once()
    reason = ks.halt.await_args.args[0]
    assert "reconciliation divergence" in reason
    assert "BTC" in reason


async def test_halts_when_exchange_has_position_cache_does_not() -> None:
    tracker = _tracker(
        positions=[],
        orders=[],
        refresh_to_positions=[_pos("ETH", Decimal("1"))],
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_awaited_once()
    assert "ETH" in ks.halt.await_args.args[0]


async def test_halts_when_cache_has_position_exchange_does_not() -> None:
    tracker = _tracker(
        positions=[_pos("BTC", Decimal("0.1"))],
        orders=[],
        refresh_to_positions=[],
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_awaited_once()


async def test_halts_when_open_orders_diverge() -> None:
    tracker = _tracker(
        positions=[],
        orders=[_order(1)],
        refresh_to_orders=[_order(2)],  # different oid → both diff types fire
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_awaited_once()
    assert "order" in ks.halt.await_args.args[0]


async def test_halts_when_refresh_raises() -> None:
    tracker = _tracker(
        positions=[],
        orders=[],
        refresh_raises=ConnectionError("network down"),
    )
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, InMemoryJournal())
    await rec.check_once()
    ks.halt.assert_awaited_once()
    assert "reconciliation failed" in ks.halt.await_args.args[0]


# ---------------------------------------------------------------------------
# Journal entries
# ---------------------------------------------------------------------------


async def test_journals_every_tick_with_diff_summary() -> None:
    tracker = _tracker(
        positions=[_pos("BTC", Decimal("0.1"))],
        orders=[],
        refresh_to_positions=[_pos("BTC", Decimal("0.5"))],
    )
    journal = InMemoryJournal()
    ks = _make_kill_switch()
    rec = Reconciler(tracker, ks, journal)
    await rec.check_once()
    ticks = [e for e in journal.events if e[1] == "reconciler_tick"]
    assert len(ticks) == 1
    payload = ticks[0][2]
    assert payload["divergences"]
    assert payload["old_positions"] == {"BTC": "0.1"}
    assert payload["new_positions"] == {"BTC": "0.5"}


async def test_journals_clean_tick_with_no_divergences() -> None:
    tracker = _tracker(positions=[_pos("BTC", Decimal("0.1"))], orders=[])
    journal = InMemoryJournal()
    rec = Reconciler(tracker, _make_kill_switch(), journal)
    await rec.check_once()
    ticks = [e for e in journal.events if e[1] == "reconciler_tick"]
    assert len(ticks) == 1
    assert ticks[0][2]["divergences"] == []


_ = pytest
