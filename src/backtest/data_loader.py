"""Historical candle loader for Hyperliquid /info candleSnapshot.

Fetches 5-minute candles for a list of coins over the last N days, paging
under the 5000-bar/call API limit. Each chunk is cached to its own
parquet file so re-runs skip already-fetched windows. Default chunk size
is 14 days (4032 bars) which leaves headroom under the limit.

Chunk boundaries are anchored to a fixed epoch so the cache key for any
given calendar window is stable across runs and across machines.

The loader is synchronous: the Hyperliquid Python SDK's `info.candles_snapshot`
uses `requests` under the hood, so wrapping in asyncio buys nothing here.
A 200ms sleep between API calls keeps us well under any rate limit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

logger = logging.getLogger(__name__)

# 2020-01-01T00:00:00Z — fixed anchor so chunk boundaries don't drift.
ANCHOR_MS: int = 1_577_836_800_000
MS_PER_DAY: int = 86_400_000

CANDLE_COLUMNS = ("open", "high", "low", "close", "volume", "num_trades")


class InfoLike(Protocol):
    """Subset of `hyperliquid.info.Info` we depend on."""

    def candles_snapshot(
        self, coin: str, interval: str, startTime: int, endTime: int
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ProgressEvent:
    coin: str
    chunk_index: int
    chunk_count: int
    chunk_start_ms: int
    chunk_end_ms: int
    cache_hit: bool
    bars: int


ProgressFn = Callable[[ProgressEvent], None]


def _chunk_windows(start_ms: int, end_ms: int, chunk_ms: int) -> list[tuple[int, int]]:
    """Return [(chunk_start, chunk_end), ...] covering [start_ms, end_ms].

    Chunks are anchored at ANCHOR_MS so a request for the same calendar
    window always produces identical (start, end) tuples regardless of
    when the call is made.
    """
    if end_ms <= start_ms:
        return []
    first_idx = (start_ms - ANCHOR_MS) // chunk_ms
    last_idx = (end_ms - ANCHOR_MS - 1) // chunk_ms
    windows: list[tuple[int, int]] = []
    for i in range(first_idx, last_idx + 1):
        chunk_start = ANCHOR_MS + i * chunk_ms
        chunk_end = chunk_start + chunk_ms
        windows.append((chunk_start, chunk_end))
    return windows


def _candles_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(
            columns=list(CANDLE_COLUMNS),
            index=pd.DatetimeIndex([], tz="UTC", name="time"),
        )
    df = pd.DataFrame(candles)
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("time")
    df = df.rename(
        columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "n": "num_trades",
        }
    )
    df = df[list(CANDLE_COLUMNS)]
    typed = df.astype(
        {
            "open": "float64",
            "high": "float64",
            "low": "float64",
            "close": "float64",
            "volume": "float64",
            "num_trades": "int64",
        }
    )
    return typed.sort_index()


def _cache_path(cache_dir: Path, coin: str, interval: str, start_ms: int, end_ms: int) -> Path:
    return cache_dir / f"{coin}_{interval}_{start_ms}_{end_ms}.parquet"


def load_candles(
    coins: list[str],
    days: int,
    info: InfoLike,
    cache_dir: Path | str,
    *,
    interval: str = "5m",
    chunk_days: int = 14,
    rate_limit_seconds: float = 0.2,
    on_progress: ProgressFn | None = None,
    now_ms: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, pd.DataFrame]:
    """Load `days` of `interval` candles for each coin, with parquet caching.

    Returns a dict keyed by coin, each value a UTC-indexed OHLCV DataFrame
    sliced to [now - days, now]. Out-of-window rows from the chunks at the
    edges are trimmed.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    start_ms = now_ms - days * MS_PER_DAY
    chunk_ms = chunk_days * MS_PER_DAY

    out: dict[str, pd.DataFrame] = {}
    for coin in coins:
        windows = _chunk_windows(start_ms, now_ms, chunk_ms)
        frames: list[pd.DataFrame] = []
        for idx, (cs, ce) in enumerate(windows):
            cache_file = _cache_path(cache_dir, coin, interval, cs, ce)
            if cache_file.exists():
                df = pd.read_parquet(cache_file)
                if on_progress is not None:
                    on_progress(ProgressEvent(coin, idx, len(windows), cs, ce, True, len(df)))
            else:
                candles = info.candles_snapshot(coin, interval, cs, ce)
                df = _candles_to_df(candles)
                df.to_parquet(cache_file)
                if on_progress is not None:
                    on_progress(ProgressEvent(coin, idx, len(windows), cs, ce, False, len(df)))
                if rate_limit_seconds > 0:
                    sleep(rate_limit_seconds)
            if not df.empty:
                frames.append(df)

        if frames:
            full = pd.concat(frames)
            full = full[~full.index.duplicated(keep="first")].sort_index()
            window_start = pd.Timestamp(start_ms, unit="ms", tz="UTC")
            window_end = pd.Timestamp(now_ms, unit="ms", tz="UTC")
            full = full.loc[(full.index >= window_start) & (full.index < window_end)]
        else:
            full = _candles_to_df([])
        out[coin] = full

    return out
