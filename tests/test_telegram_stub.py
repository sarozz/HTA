"""Tests for src/notify/telegram.py stub + command router."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.notify.telegram import CommandRouter, TelegramNotifier
from src.storage.journal import InMemoryJournal


async def test_send_logs_to_stdout_and_journal(capsys: pytest.CaptureFixture[str]) -> None:
    journal = InMemoryJournal()
    notifier = TelegramNotifier(journal)

    await notifier.send("hello")

    out = capsys.readouterr().out
    assert "hello" in out and "[telegram " in out
    assert any(e[1] == "telegram_out" and e[2]["message"] == "hello" for e in journal.events)


async def test_command_router_dispatches_halt_resume_status() -> None:
    journal = InMemoryJournal()
    ks = AsyncMock()
    ks.halted = False
    status = AsyncMock()
    status.status = AsyncMock(return_value="equity=$1500\npositions=0")
    router = CommandRouter(ks, status, journal)

    out = await router.dispatch("/halt market closed")
    ks.halt.assert_awaited_once_with("market closed")
    assert "halted" in out

    out = await router.dispatch("/resume")
    ks.resume.assert_awaited_once()
    assert out == "resumed"

    ks.halted = True
    out = await router.dispatch("/status")
    assert "HALTED" in out and "equity=$1500" in out


async def test_command_router_unknown_returns_usage() -> None:
    journal = InMemoryJournal()
    ks = AsyncMock()
    ks.halted = False
    router = CommandRouter(ks, AsyncMock(), journal)

    out = await router.dispatch("/foo")
    assert "unknown" in out

    out = await router.dispatch("")
    assert "empty" in out


async def test_command_router_journals_inbound() -> None:
    journal = InMemoryJournal()
    ks = AsyncMock()
    ks.halted = False
    router = CommandRouter(ks, AsyncMock(status=AsyncMock(return_value="ok")), journal)

    await router.dispatch("/status")
    assert any(e[1] == "telegram_in" and e[2]["command"] == "/status" for e in journal.events)
