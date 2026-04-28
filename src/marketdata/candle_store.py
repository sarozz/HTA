"""In-memory OHLCV candle store built from the trade stream.

Buckets trades into UTC-aligned bars of `bar_seconds` (default 5 minutes)
and keeps the last `history` bars per symbol.

Hyperliquid trade message shape (per element of the `trades` channel data):
    {"coin": "BTC", "side": "A"|"B", "px": "30000.0", "sz": "0.001",
     "time": 1700000000000, "hash": "0x..."}

`on_trade` accepts the parsed fields directly so the store is decoupled
from the WS dispatcher. `on_trades_message` is a convenience that ingests
the raw data list from the "trades" channel.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Bar:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleStore:
    def __init__(self, history: int = 200, bar_seconds: int = 300) -> None:
        if bar_seconds <= 0:
            raise ValueError("bar_seconds must be positive")
        if history <= 0:
            raise ValueError("history must be positive")
        self._history = history
        self._bar_seconds = bar_seconds
        self._bar_ms = bar_seconds * 1000
        self._bars: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=self._history))

    @property
    def bar_seconds(self) -> int:
        return self._bar_seconds

    def on_trade(self, coin: str, px: float, sz: float, time_ms: int) -> None:
        bar_open = (time_ms // self._bar_ms) * self._bar_ms
        bars = self._bars[coin]

        if not bars or bars[-1].open_time_ms < bar_open:
            bars.append(
                Bar(
                    open_time_ms=bar_open,
                    open=px,
                    high=px,
                    low=px,
                    close=px,
                    volume=sz,
                )
            )
            return

        last = bars[-1]
        if last.open_time_ms == bar_open:
            bars[-1] = Bar(
                open_time_ms=last.open_time_ms,
                open=last.open,
                high=max(last.high, px),
                low=min(last.low, px),
                close=px,
                volume=last.volume + sz,
            )
            return

        # Out-of-order trade for an older bar — ignore. Hyperliquid normally
        # delivers in order; tolerating disorder here would corrupt past bars.

    def on_trades_message(self, data: list[dict[str, Any]]) -> None:
        for trade in data:
            self.on_trade(
                coin=trade["coin"],
                px=float(trade["px"]),
                sz=float(trade["sz"]),
                time_ms=int(trade["time"]),
            )

    def get_candles(self, symbol: str) -> pd.DataFrame:
        bars = self._bars.get(symbol)
        cols = ["open", "high", "low", "close", "volume"]
        if not bars:
            return pd.DataFrame(
                columns=cols,
                index=pd.DatetimeIndex([], tz="UTC", name="time"),
            )
        idx = pd.to_datetime([b.open_time_ms for b in bars], unit="ms", utc=True)
        idx.name = "time"
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=idx,
        )

    def symbols(self) -> list[str]:
        return [s for s, bars in self._bars.items() if bars]
