"""Tests for KillSwitch.halt() / resume() with a mocked exchange adapter."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.notify.telegram import TelegramNotifier
from src.risk.kill_switch import KillSwitch, OpenOrderRef, PositionSnapshot
from src.storage.journal import InMemoryJournal


def _make_adapter(
    open_orders: list[OpenOrderRef] | None = None,
    positions: list[PositionSnapshot] | None = None,
) -> AsyncMock:
    adapter = AsyncMock()
    adapter.list_open_orders = AsyncMock(return_value=list(open_orders or []))
    adapter.list_positions = AsyncMock(return_value=list(positions or []))
    adapter.cancel = AsyncMock(return_value=None)
    adapter.market_close = AsyncMock(return_value=None)
    return adapter


async def test_halt_cancels_orders_closes_positions_and_journals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _make_adapter(
        open_orders=[
            OpenOrderRef("BTC", 1),
            OpenOrderRef("ETH", 2),
            OpenOrderRef("SOL", 3),
        ],
        positions=[
            PositionSnapshot("BTC", Decimal("0.01")),
            PositionSnapshot("ETH", Decimal("-0.5")),
            PositionSnapshot("SOL", Decimal("0")),  # zero-size: skipped
        ],
    )
    journal = InMemoryJournal()
    notifier = TelegramNotifier(journal)
    ks = KillSwitch(adapter, journal, notifier)

    assert ks.halted is False
    await ks.halt("ws silent > 30s")
    assert ks.halted is True

    # All three orders cancelled.
    cancel_calls = sorted(c.args for c in adapter.cancel.await_args_list)
    assert cancel_calls == [("BTC", 1), ("ETH", 2), ("SOL", 3)]

    # Only non-zero positions closed.
    close_calls = sorted(c.args[0] for c in adapter.market_close.await_args_list)
    assert close_calls == ["BTC", "ETH"]
    assert adapter.market_close.await_count == 2

    # Journal has a halt event with the reason and the closed/cancelled lists.
    halt_events = [e for e in journal.events if e[1] == "halt"]
    assert len(halt_events) == 1
    payload = halt_events[0][2]
    assert payload["reason"] == "ws silent > 30s"
    assert "ts" in payload
    assert payload["closed_positions"] == ["BTC", "ETH"]
    assert sorted((o["symbol"], o["oid"]) for o in payload["cancelled_orders"]) == [
        ("BTC", 1),
        ("ETH", 2),
        ("SOL", 3),
    ]
    assert payload["cancel_errors"] == []
    assert payload["close_errors"] == []

    # Telegram stub also wrote the outbound notification.
    out_events = [e for e in journal.events if e[1] == "telegram_out"]
    assert any("HALTED: ws silent > 30s" in e[2]["message"] for e in out_events)
    captured = capsys.readouterr()
    assert "HALTED: ws silent > 30s" in captured.out


async def test_halt_is_idempotent() -> None:
    adapter = _make_adapter(open_orders=[OpenOrderRef("BTC", 1)])
    journal = InMemoryJournal()
    notifier = TelegramNotifier(journal)
    ks = KillSwitch(adapter, journal, notifier)

    await ks.halt("first")
    await ks.halt("second")  # no-op

    adapter.cancel.assert_awaited_once()
    halt_events = [e for e in journal.events if e[1] == "halt"]
    assert len(halt_events) == 1
    assert halt_events[0][2]["reason"] == "first"


async def test_halt_continues_when_cancel_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _make_adapter(
        open_orders=[OpenOrderRef("BTC", 1), OpenOrderRef("ETH", 2)],
        positions=[PositionSnapshot("BTC", Decimal("0.01"))],
    )
    adapter.cancel.side_effect = [RuntimeError("boom"), None]
    journal = InMemoryJournal()
    ks = KillSwitch(adapter, journal, TelegramNotifier(journal))

    await ks.halt("error path")

    assert ks.halted is True
    # Both cancels attempted; market_close still ran.
    assert adapter.cancel.await_count == 2
    adapter.market_close.assert_awaited_once_with("BTC")
    payload = next(e for e in journal.events if e[1] == "halt")[2]
    assert any("cancel(BTC,1)" in err for err in payload["cancel_errors"])
    assert payload["close_errors"] == []


async def test_resume_clears_flag_and_journals(capsys: pytest.CaptureFixture[str]) -> None:
    adapter = _make_adapter()
    journal = InMemoryJournal()
    notifier = TelegramNotifier(journal)
    ks = KillSwitch(adapter, journal, notifier)

    await ks.halt("dummy")
    assert ks.halted is True

    await ks.resume()
    assert ks.halted is False

    resume_events = [e for e in journal.events if e[1] == "resume"]
    assert len(resume_events) == 1

    out = [e for e in journal.events if e[1] == "telegram_out" and e[2]["message"] == "Resumed"]
    assert len(out) == 1


async def test_resume_when_not_halted_is_noop() -> None:
    adapter = _make_adapter()
    journal = InMemoryJournal()
    ks = KillSwitch(adapter, journal, TelegramNotifier(journal))

    await ks.resume()
    assert ks.halted is False
    assert [e for e in journal.events if e[1] == "resume"] == []
