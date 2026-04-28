"""Tests for src/backtest/data_loader.py.

The Hyperliquid SDK is replaced by a fake `InfoLike` that records every
call. Verifies:
  - chunking covers the requested window (1, 2, 3+ chunks)
  - chunk boundaries are anchored (re-running with the same window
    produces the same chunk tuples)
  - on cache hit, no API call; on miss, parquet is written
  - rate-limit sleep is observed only on miss
  - candle JSON is parsed into a tz-aware OHLCV DataFrame
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.backtest.data_loader import (
    ANCHOR_MS,
    MS_PER_DAY,
    ProgressEvent,
    _candles_to_df,
    _chunk_windows,
    load_candles,
)

CHUNK_DAYS = 14
CHUNK_MS = CHUNK_DAYS * MS_PER_DAY
BAR_MS = 300_000  # 5 minutes


def _make_candles(start_ms: int, end_ms: int, base_price: float = 100.0) -> list[dict[str, Any]]:
    """Synthesise a candle list at 5m intervals covering [start_ms, end_ms)."""
    out: list[dict[str, Any]] = []
    t = start_ms
    while t < end_ms:
        out.append(
            {
                "t": t,
                "T": t + BAR_MS,
                "s": "BTC",
                "i": "5m",
                "o": str(base_price),
                "h": str(base_price + 1),
                "l": str(base_price - 1),
                "c": str(base_price + 0.5),
                "v": "1.0",
                "n": 5,
            }
        )
        t += BAR_MS
    return out


class FakeInfo:
    def __init__(self, base_price: float = 100.0) -> None:
        self.calls: list[tuple[str, str, int, int]] = []
        self.base_price = base_price

    def candles_snapshot(
        self, coin: str, interval: str, startTime: int, endTime: int
    ) -> list[dict[str, Any]]:
        self.calls.append((coin, interval, startTime, endTime))
        return _make_candles(startTime, endTime, self.base_price)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_chunk_windows_anchored_to_fixed_epoch() -> None:
    # Same calendar window from two "now"s yields identical chunk tuples
    # because boundaries are anchored at ANCHOR_MS.
    base = ANCHOR_MS + 100 * CHUNK_MS  # somewhere safely past the anchor
    a = _chunk_windows(base, base + 30 * MS_PER_DAY, CHUNK_MS)
    b = _chunk_windows(base, base + 30 * MS_PER_DAY, CHUNK_MS)
    assert a == b
    # Each chunk start must be aligned to the anchor.
    for cs, ce in a:
        assert (cs - ANCHOR_MS) % CHUNK_MS == 0
        assert ce - cs == CHUNK_MS


def test_chunk_windows_covers_full_range_with_overlap_at_edges() -> None:
    # 30-day window -> at least 3 chunks of 14 days that together cover it.
    start = ANCHOR_MS + 100 * CHUNK_MS + 5 * MS_PER_DAY
    end = start + 30 * MS_PER_DAY
    windows = _chunk_windows(start, end, CHUNK_MS)
    assert windows[0][0] <= start
    assert windows[-1][1] >= end
    assert len(windows) >= 3


def test_chunk_windows_handles_empty_range() -> None:
    assert _chunk_windows(1000, 1000, CHUNK_MS) == []
    assert _chunk_windows(2000, 1000, CHUNK_MS) == []


# ---------------------------------------------------------------------------
# _candles_to_df parsing
# ---------------------------------------------------------------------------


def test_candles_to_df_parses_types_and_index() -> None:
    raw = _make_candles(ANCHOR_MS, ANCHOR_MS + 3 * BAR_MS)
    df = _candles_to_df(raw)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "num_trades"]
    assert df.dtypes["open"] == "float64"
    assert df.dtypes["num_trades"] == "int64"
    assert str(df.index.tz) == "UTC"
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp(ANCHOR_MS, unit="ms", tz="UTC")


def test_candles_to_df_empty_input_returns_typed_empty_frame() -> None:
    df = _candles_to_df([])
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "num_trades"]
    assert str(df.index.tz) == "UTC"


# ---------------------------------------------------------------------------
# load_candles end-to-end
# ---------------------------------------------------------------------------


def test_load_candles_chunks_window_and_returns_combined_frame(tmp_path: Path) -> None:
    info = FakeInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS  # some future point
    sleeps: list[float] = []

    out = load_candles(
        coins=["BTC"],
        days=30,
        info=info,
        cache_dir=tmp_path,
        rate_limit_seconds=0.2,
        now_ms=now_ms,
        sleep=sleeps.append,
    )

    assert "BTC" in out
    df = out["BTC"]
    # The 30-day window slices to [now-30d, now); first/last bars must lie within.
    assert df.index[0] >= pd.Timestamp(now_ms - 30 * MS_PER_DAY, unit="ms", tz="UTC")
    assert df.index[-1] < pd.Timestamp(now_ms, unit="ms", tz="UTC")
    # 30 days at 5-min bars = 8640 bars, but trimmed to the actual window
    # boundaries — should be ≤ 8640.
    assert len(df) <= 30 * MS_PER_DAY // BAR_MS

    # At least two chunk calls (30-day window straddles ≥3 14-day windows).
    assert len(info.calls) >= 2
    # One sleep per call (cache miss path).
    assert len(sleeps) == len(info.calls)
    assert all(math.isclose(s, 0.2) for s in sleeps)


def test_load_candles_cache_hit_skips_api_and_sleep(tmp_path: Path) -> None:
    info_first = FakeInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS

    # First run: populate cache.
    sleeps_a: list[float] = []
    load_candles(
        coins=["BTC"],
        days=30,
        info=info_first,
        cache_dir=tmp_path,
        rate_limit_seconds=0.2,
        now_ms=now_ms,
        sleep=sleeps_a.append,
    )
    first_call_count = len(info_first.calls)
    assert first_call_count >= 2

    # Cache files should exist.
    parquet_files = list(tmp_path.glob("BTC_5m_*.parquet"))
    assert len(parquet_files) == first_call_count

    # Second run: identical args. All chunks should be cached.
    info_second = FakeInfo()
    sleeps_b: list[float] = []
    out2 = load_candles(
        coins=["BTC"],
        days=30,
        info=info_second,
        cache_dir=tmp_path,
        rate_limit_seconds=0.2,
        now_ms=now_ms,
        sleep=sleeps_b.append,
    )
    assert info_second.calls == []  # zero API calls
    assert sleeps_b == []  # no sleeps because no fetches
    assert not out2["BTC"].empty


def test_load_candles_mixes_cache_and_fetch_for_partial_overlap(tmp_path: Path) -> None:
    """If only some chunks are cached, the loader fetches the missing ones."""
    info = FakeInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS

    # First run for 14 days -> caches one chunk window.
    load_candles(
        coins=["BTC"],
        days=14,
        info=info,
        cache_dir=tmp_path,
        rate_limit_seconds=0.0,
        now_ms=now_ms,
    )
    initial_calls = len(info.calls)

    # Second run for 30 days from same `now_ms`. The chunk that was just
    # cached must be re-used; new chunks must be fetched.
    info2 = FakeInfo()
    load_candles(
        coins=["BTC"],
        days=30,
        info=info2,
        cache_dir=tmp_path,
        rate_limit_seconds=0.0,
        now_ms=now_ms,
    )
    # All chunks for the 30-day window minus the ones already cached.
    chunks_30 = _chunk_windows(now_ms - 30 * MS_PER_DAY, now_ms, CHUNK_MS)
    chunks_14 = _chunk_windows(now_ms - 14 * MS_PER_DAY, now_ms, CHUNK_MS)
    assert len(info2.calls) == len(chunks_30) - len(chunks_14)
    # Sanity: the first run's calls all matched the 14-day chunks.
    assert initial_calls == len(chunks_14)


def test_load_candles_progress_callback_fires_per_chunk(tmp_path: Path) -> None:
    info = FakeInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS
    events: list[ProgressEvent] = []

    load_candles(
        coins=["BTC"],
        days=30,
        info=info,
        cache_dir=tmp_path,
        rate_limit_seconds=0.0,
        now_ms=now_ms,
        on_progress=events.append,
    )

    assert len(events) == len(info.calls)
    # All on first run are cache misses.
    assert all(not e.cache_hit for e in events)
    # Each event has bars > 0 (we synthesised real candles).
    assert all(e.bars > 0 for e in events)
    # chunk_index goes 0..n-1 for the BTC coin.
    assert [e.chunk_index for e in events] == list(range(len(events)))


def test_load_candles_rejects_invalid_args(tmp_path: Path) -> None:
    info = FakeInfo()
    with pytest.raises(ValueError):
        load_candles(coins=["BTC"], days=0, info=info, cache_dir=tmp_path)
    with pytest.raises(ValueError):
        load_candles(coins=["BTC"], days=30, info=info, cache_dir=tmp_path, chunk_days=0)


def test_load_candles_handles_multiple_symbols_independently(tmp_path: Path) -> None:
    info = FakeInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS

    out = load_candles(
        coins=["BTC", "ETH"],
        days=14,
        info=info,
        cache_dir=tmp_path,
        rate_limit_seconds=0.0,
        now_ms=now_ms,
    )

    assert set(out.keys()) == {"BTC", "ETH"}
    btc_calls = [c for c in info.calls if c[0] == "BTC"]
    eth_calls = [c for c in info.calls if c[0] == "ETH"]
    assert len(btc_calls) == len(eth_calls)
    assert len(btc_calls) >= 1


def test_load_candles_dedupes_overlapping_rows(tmp_path: Path) -> None:
    """If chunk edges overlap by one bar, the loader keeps the row only once."""

    class OverlappingInfo(FakeInfo):
        def candles_snapshot(self, coin, interval, startTime, endTime):
            # Return one bar of overlap with the next chunk.
            return _make_candles(startTime, endTime + BAR_MS, self.base_price)

    info = OverlappingInfo()
    now_ms = ANCHOR_MS + 200 * CHUNK_MS

    out = load_candles(
        coins=["BTC"],
        days=14,
        info=info,
        cache_dir=tmp_path,
        rate_limit_seconds=0.0,
        now_ms=now_ms,
    )
    df = out["BTC"]
    assert df.index.is_unique
