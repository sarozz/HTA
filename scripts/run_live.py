"""Run the live event loop on Hyperliquid TESTNET.

Read-only by design *unless* the environment provides an API wallet key
that's authorised to trade. Even then, this is testnet only — the URL
is hardcoded inside src/main.py.

Usage (on the user's machine, not in the sandbox):

    cp .env.example .env
    # fill in HL_API_KEY (API wallet key, NOT main wallet) and
    # HL_ACCOUNT_ADDRESS (the master account address).
    python -m scripts.run_live

The script prints a heartbeat line every 5 seconds so you can see the
event loop ticking, plus one-line summaries of every journal event.
Ctrl+C to stop; the loop drains cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from pathlib import Path

from src.main import build_system

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _heartbeat(system: object, stop_event: asyncio.Event) -> None:
    n = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass
        n += 1
        kill_switch = getattr(system, "_kill", None)
        tracker = getattr(system, "_tracker", None)
        halted = bool(getattr(kill_switch, "halted", False)) if kill_switch else False
        equity = str(tracker.equity) if tracker is not None else "?"
        positions = len(tracker.positions()) if tracker is not None else 0
        sys.stdout.write(
            f"[heartbeat #{n:04d}] halted={halted}  equity={equity}  positions={positions}\n"
        )
        sys.stdout.flush()


async def main_async() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config_path = REPO_ROOT / "config" / "example.yaml"
    print(f"booting LiveSystem (testnet) with config {config_path}")
    system = await build_system(config_path)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: (stop_event.set(), system.stop()))

    heartbeat = asyncio.create_task(_heartbeat(system, stop_event), name="heartbeat")
    try:
        rc = await system.run()
    finally:
        stop_event.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    print(f"LiveSystem exited rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
