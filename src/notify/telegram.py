"""Telegram notifier — stub for now.

Two pieces:
  - TelegramNotifier: implements the Notifier protocol used by the kill
    switch and other producers. Logs to stdout and journals the message.
  - CommandRouter: scaffold for inbound /halt, /resume, /status commands.
    A real bot loop will plug into this later.

The actual python-telegram-bot client lands in a later session; nothing
here makes a network call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Protocol

from src.storage.journal import JournalLike

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, message: str) -> None: ...


class StatusProvider(Protocol):
    async def status(self) -> str: ...


class HaltController(Protocol):
    """Subset of KillSwitch the command router needs."""

    @property
    def halted(self) -> bool: ...

    async def halt(self, reason: str) -> None: ...

    async def resume(self) -> None: ...


class TelegramNotifier:
    """Stub Notifier. Prints to stdout and writes to the journal."""

    def __init__(self, journal: JournalLike) -> None:
        self._journal = journal

    async def send(self, message: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        logger.info("telegram %s %s", ts, message)
        print(f"[telegram {ts}] {message}")
        await self._journal.append("telegram_out", {"ts": ts, "message": message})


CommandHandler = Callable[[str], Awaitable[str]]


class CommandRouter:
    """Routes /halt, /resume, /status commands to a kill-switch + status source.

    Unknown commands return a short usage string. The router doesn't talk to
    Telegram itself — a future bot loop will read messages and call dispatch.
    """

    def __init__(
        self,
        kill_switch: HaltController,
        status_provider: StatusProvider,
        journal: JournalLike,
    ) -> None:
        self._ks = kill_switch
        self._status = status_provider
        self._journal = journal
        self._handlers: dict[str, CommandHandler] = {
            "/halt": self._on_halt,
            "/resume": self._on_resume,
            "/status": self._on_status,
        }

    async def dispatch(self, raw: str) -> str:
        text = raw.strip()
        if not text:
            return "empty command"
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        await self._journal.append("telegram_in", {"command": cmd, "arg": arg})
        handler = self._handlers.get(cmd)
        if handler is None:
            return "unknown command. supported: /halt, /resume, /status"
        return await handler(arg)

    async def _on_halt(self, arg: str) -> str:
        reason = arg or "manual halt via Telegram"
        await self._ks.halt(reason)
        return f"halted: {reason}"

    async def _on_resume(self, _: str) -> str:
        await self._ks.resume()
        return "resumed"

    async def _on_status(self, _: str) -> str:
        body = await self._status.status()
        flag = "HALTED" if self._ks.halted else "running"
        return f"{flag}\n{body}"
