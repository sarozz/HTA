"""Print live BTC-PERP 5m candles from Hyperliquid testnet for 60 seconds.

Read-only; no orders are placed. Always uses testnet WebSocket regardless
of environment per CLAUDE.md non-negotiable #1.

Output every 5 seconds:
  - the latest in-progress candle (open/high/low/close/volume + bar time)
  - a summary line for any closed candles since the last tick
  - 'STALE' if the WebSocket has gone silent for >30s
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

from src.marketdata.candle_store import CandleStore
from src.marketdata.ws_client import (
    CHANNEL_CONNECTED,
    CHANNEL_DISCONNECTED,
    CHANNEL_STALE,
    CHANNEL_TRADES,
    HyperliquidWsClient,
)

TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
SYMBOL = "BTC"  # Hyperliquid uses bare coin names; "BTC-PERP" in output only
RUN_SECONDS = 60.0
PRINT_EVERY_SECONDS = 5.0


def _fmt_bar(idx: Any, row: Any) -> str:
    return (
        f"BTC-PERP {idx.isoformat()}  "
        f"O={row.open:>10.2f}  H={row.high:>10.2f}  "
        f"L={row.low:>10.2f}  C={row.close:>10.2f}  V={row.volume:.4f}"
    )


async def _print_loop(store: CandleStore, stop: asyncio.Event) -> None:
    last_closed_open_time: Any = None
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PRINT_EVERY_SECONDS)
            return  # stop was set
        except asyncio.TimeoutError:
            pass

        df = store.get_candles(SYMBOL)
        if df.empty:
            print("(no candles yet — waiting for first trade)")
            continue

        # Print any newly-closed bars first (everything except the last row).
        for idx, row in df.iloc[:-1].iterrows():
            if last_closed_open_time is None or idx > last_closed_open_time:
                print(f"closed: {_fmt_bar(idx, row)}")
                last_closed_open_time = idx

        # Then the in-progress (latest) bar.
        idx = df.index[-1]
        row = df.iloc[-1]
        print(f"open  : {_fmt_bar(idx, row)}")


async def main() -> int:
    store = CandleStore(history=20, bar_seconds=300)
    client = HyperliquidWsClient(TESTNET_WS_URL, [SYMBOL])

    client.on(CHANNEL_TRADES, lambda data: store.on_trades_message(data))
    client.on(
        CHANNEL_CONNECTED,
        lambda data: print(f"connected: {data['url']} at {datetime.now(timezone.utc).isoformat()}"),
    )
    client.on(CHANNEL_DISCONNECTED, lambda data: print(f"disconnected: {data['error']}"))
    client.on(
        CHANNEL_STALE,
        lambda data: print(f"STALE: no message for {data['elapsed']:.1f}s"),
    )

    stop = asyncio.Event()
    run_task = asyncio.create_task(client.run(), name="ws-run")
    print_task = asyncio.create_task(_print_loop(store, stop), name="print-loop")

    print(f"streaming BTC-PERP from {TESTNET_WS_URL} for {RUN_SECONDS:.0f}s...")
    try:
        await asyncio.sleep(RUN_SECONDS)
    finally:
        client.stop()
        stop.set()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        with contextlib.suppress(asyncio.CancelledError):
            await print_task

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
