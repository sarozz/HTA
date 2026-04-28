"""Pulls historical Strategy B inputs from Hyperliquid /info.

Two data products:

  fetch_funding_history(...)   -> DataFrame[tick_time, coin, funding_rate]
  fetch_candles_around(...)    -> DataFrame[time, open, high, low, close, volume]

Both write parquet files into data/cache/ keyed by (coin, start, end) so
re-runs skip already-fetched windows.

The Hyperliquid SDK is synchronous, so we wrap the Info object behind a
small Protocol and call it directly. The simulator and tests do not
import this module — they consume DataFrames.

Per CLAUDE.md non-negotiable #1, the default base URL is testnet.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache")
RATE_LIMIT_SLEEP_SECONDS = 0.20

# Hyperliquid funding history: max ~500 entries per call (per docs).
# 500 hourly ticks ≈ 20 days; we request in 14-day windows to leave margin.
FUNDING_WINDOW_DAYS = 14

# candleSnapshot also has a per-call cap. 1m candles in 30-minute windows
# is 30 candles per tick, well below any limit. We just go tick-by-tick.


class HyperliquidInfo(Protocol):
    def funding_history(self, name: str, startTime: int, endTime: int | None = ...) -> Any: ...
    def candles_snapshot(self, coin: str, interval: str, startTime: int, endTime: int) -> Any: ...


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------
# Funding history
# --------------------------------------------------------------------------


def fetch_funding_history(
    info: HyperliquidInfo,
    coin: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    sleep_seconds: float = RATE_LIMIT_SLEEP_SECONDS,
) -> pd.DataFrame:
    """Return all funding ticks for `coin` in [start, end). Cached per window."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=FUNDING_WINDOW_DAYS), end)
        cache_key = f"funding_{coin}_{_ms(cursor)}_{_ms(window_end)}.parquet"
        cache_path = cache_dir / cache_key
        if cache_path.exists():
            logger.info("cache hit: %s", cache_path.name)
            df = pd.read_parquet(cache_path)
        else:
            logger.info("fetch funding %s [%s, %s)", coin, cursor, window_end)
            raw = info.funding_history(coin, _ms(cursor), _ms(window_end))
            df = _funding_raw_to_df(raw or [])
            df.to_parquet(cache_path, index=False)
            time.sleep(sleep_seconds)
        rows.append(df)
        cursor = window_end
    if not rows:
        return _empty_funding_df()
    out = pd.concat(rows, ignore_index=True)
    out = out.drop_duplicates(subset=["tick_time", "coin"]).sort_values("tick_time")
    out = out.reset_index(drop=True)
    return out


def fetch_funding_history_for_coins(
    info: HyperliquidInfo,
    coins: list[str],
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    sleep_seconds: float = RATE_LIMIT_SLEEP_SECONDS,
) -> pd.DataFrame:
    """Concat funding for multiple coins."""
    parts: list[pd.DataFrame] = []
    for coin in coins:
        parts.append(
            fetch_funding_history(
                info, coin, start, end, cache_dir=cache_dir, sleep_seconds=sleep_seconds
            )
        )
    if not parts:
        return _empty_funding_df()
    return pd.concat(parts, ignore_index=True).sort_values("tick_time").reset_index(drop=True)


def _funding_raw_to_df(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return _empty_funding_df()
    df = pd.DataFrame(
        [
            {
                "tick_time": pd.to_datetime(int(r["time"]), unit="ms", utc=True),
                "coin": str(r["coin"]),
                "funding_rate": float(r["fundingRate"]),
                "premium": float(r.get("premium", 0.0)),
            }
            for r in raw
        ]
    )
    return df


def _empty_funding_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "tick_time": pd.to_datetime([], utc=True),
            "coin": pd.Series([], dtype="object"),
            "funding_rate": pd.Series([], dtype="float64"),
            "premium": pd.Series([], dtype="float64"),
        }
    )


# --------------------------------------------------------------------------
# 1m candles around a funding tick
# --------------------------------------------------------------------------


def fetch_candles_around(
    info: HyperliquidInfo,
    coin: str,
    tick_time: datetime,
    *,
    window_minutes: int = 15,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    sleep_seconds: float = RATE_LIMIT_SLEEP_SECONDS,
) -> pd.DataFrame:
    """1-minute candles for `coin` in [tick_time-window, tick_time+window)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    start = tick_time - timedelta(minutes=window_minutes)
    end = tick_time + timedelta(minutes=window_minutes)
    cache_key = f"candles1m_{coin}_{_ms(start)}_{_ms(end)}.parquet"
    cache_path = cache_dir / cache_key
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    raw = info.candles_snapshot(coin, "1m", _ms(start), _ms(end))
    df = _candles_raw_to_df(raw or [])
    df.to_parquet(cache_path, index=False)
    time.sleep(sleep_seconds)
    return df


def _candles_raw_to_df(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(
            {
                "time": pd.to_datetime([], utc=True),
                "open": pd.Series([], dtype="float64"),
                "high": pd.Series([], dtype="float64"),
                "low": pd.Series([], dtype="float64"),
                "close": pd.Series([], dtype="float64"),
                "volume": pd.Series([], dtype="float64"),
            }
        )
    return pd.DataFrame(
        [
            {
                "time": pd.to_datetime(int(c["t"]), unit="ms", utc=True),
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
            }
            for c in raw
        ]
    )
