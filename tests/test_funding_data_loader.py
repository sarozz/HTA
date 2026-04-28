"""Tests for the Strategy B data loader. Network is mocked; no I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.backtest.funding_data_loader import (
    fetch_candles_around,
    fetch_funding_history,
    fetch_funding_history_for_coins,
)


def _funding_response(coin: str, n_hours: int, start: datetime) -> list[dict]:
    return [
        {
            "coin": coin,
            "fundingRate": str(0.0001 + (i % 5) * 0.0001),
            "premium": "0",
            "time": int((start + timedelta(hours=i)).timestamp() * 1000),
        }
        for i in range(n_hours)
    ]


def _candles_response(start: datetime, n_minutes: int) -> list[dict]:
    return [
        {
            "t": int((start + timedelta(minutes=i)).timestamp() * 1000),
            "o": 100.0 + i * 0.01,
            "h": 100.5 + i * 0.01,
            "l": 99.5 + i * 0.01,
            "c": 100.2 + i * 0.01,
            "v": 1.5,
        }
        for i in range(n_minutes)
    ]


# ---------------------------------------------------------------------------
# Funding history
# ---------------------------------------------------------------------------


def test_fetch_funding_history_caches_to_parquet(tmp_path) -> None:
    info = MagicMock()
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=10)
    info.funding_history.return_value = _funding_response("BTC", 24 * 10, start)

    df = fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    assert len(df) == 24 * 10
    assert {"tick_time", "coin", "funding_rate", "premium"} <= set(df.columns)
    cached = list(tmp_path.glob("funding_BTC_*.parquet"))
    assert cached, "expected parquet cache file"


def test_fetch_funding_history_uses_cache_on_second_call(tmp_path) -> None:
    info = MagicMock()
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=10)
    info.funding_history.return_value = _funding_response("BTC", 24 * 10, start)

    df1 = fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    df2 = fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    assert info.funding_history.call_count == 1  # second call is cache hit
    assert len(df1) == len(df2)


def test_fetch_funding_history_paginates_in_14_day_windows(tmp_path) -> None:
    info = MagicMock()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    info.funding_history.return_value = []

    fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    # 30 days / 14 days per window = 3 calls (14, 14, 2).
    assert info.funding_history.call_count == 3


def test_fetch_funding_history_for_coins_concats(tmp_path) -> None:
    info = MagicMock()
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=2)

    def _side_effect(coin, _start, _end):
        return _funding_response(coin, 48, start)

    info.funding_history.side_effect = _side_effect

    df = fetch_funding_history_for_coins(
        info, ["BTC", "ETH", "SOL"], start, end, cache_dir=tmp_path, sleep_seconds=0
    )
    assert df["coin"].nunique() == 3
    assert len(df) == 48 * 3
    assert df["tick_time"].is_monotonic_increasing


def test_fetch_funding_history_handles_empty_response(tmp_path) -> None:
    info = MagicMock()
    info.funding_history.return_value = []
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=2)
    df = fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    assert df.empty
    assert {"tick_time", "coin", "funding_rate"} <= set(df.columns)


def test_fetch_funding_history_drops_duplicate_ticks(tmp_path) -> None:
    """If two adjacent windows overlap (rare edge), duplicate (time, coin)
    pairs are deduplicated in the final DataFrame."""
    info = MagicMock()
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=20)
    response = _funding_response("BTC", 24, start)
    info.funding_history.return_value = response  # same response for every window

    df = fetch_funding_history(info, "BTC", start, end, cache_dir=tmp_path, sleep_seconds=0)
    assert len(df) == 24  # deduplicated


# ---------------------------------------------------------------------------
# 1m candles
# ---------------------------------------------------------------------------


def test_fetch_candles_around_pulls_30_minute_window(tmp_path) -> None:
    info = MagicMock()
    tick_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    info.candles_snapshot.return_value = _candles_response(tick_time - timedelta(minutes=15), 30)

    df = fetch_candles_around(info, "BTC", tick_time, cache_dir=tmp_path, sleep_seconds=0)
    info.candles_snapshot.assert_called_once()
    args = info.candles_snapshot.call_args.args
    assert args[0] == "BTC"
    assert args[1] == "1m"
    # start = tick - 15m, end = tick + 15m
    expected_start = int((tick_time - timedelta(minutes=15)).timestamp() * 1000)
    expected_end = int((tick_time + timedelta(minutes=15)).timestamp() * 1000)
    assert args[2] == expected_start
    assert args[3] == expected_end
    assert len(df) == 30


def test_fetch_candles_around_caches(tmp_path) -> None:
    info = MagicMock()
    tick_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    info.candles_snapshot.return_value = _candles_response(tick_time - timedelta(minutes=15), 30)
    fetch_candles_around(info, "BTC", tick_time, cache_dir=tmp_path, sleep_seconds=0)
    fetch_candles_around(info, "BTC", tick_time, cache_dir=tmp_path, sleep_seconds=0)
    assert info.candles_snapshot.call_count == 1


def test_fetch_candles_around_returns_dataframe_with_expected_columns(tmp_path) -> None:
    info = MagicMock()
    tick_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    info.candles_snapshot.return_value = _candles_response(tick_time - timedelta(minutes=15), 5)
    df = fetch_candles_around(info, "BTC", tick_time, cache_dir=tmp_path, sleep_seconds=0)
    assert {"time", "open", "high", "low", "close", "volume"} <= set(df.columns)
    assert pd.api.types.is_datetime64_any_dtype(df["time"])


def test_fetch_candles_around_handles_empty_response(tmp_path) -> None:
    info = MagicMock()
    info.candles_snapshot.return_value = []
    tick_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    df = fetch_candles_around(info, "BTC", tick_time, cache_dir=tmp_path, sleep_seconds=0)
    assert df.empty
    assert {"time", "open", "high", "low", "close"} <= set(df.columns)


_ = pytest
