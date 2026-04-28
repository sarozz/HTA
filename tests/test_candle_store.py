"""Tests for CandleStore: OHLCV correctness, bar boundaries, history bound."""

from __future__ import annotations

import pandas as pd
import pytest

from src.marketdata.candle_store import CandleStore

# 5-minute bars; bar boundaries land at multiples of 300_000 ms (UTC epoch).
BAR_MS = 300_000


def test_single_trade_creates_a_bar_with_all_fields_equal_to_trade() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    store.on_trade("BTC", px=100.0, sz=0.5, time_ms=1000)
    df = store.get_candles("BTC")
    assert len(df) == 1
    row = df.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (100.0, 100.0, 100.0, 100.0, 0.5)
    # Bar opens at 0 ms (the floor of 1000 to a 5-min boundary).
    assert df.index[0] == pd.Timestamp(0, unit="ms", tz="UTC")


def test_multiple_trades_in_one_bar_compute_correct_ohlcv() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    # All within bar 0 (0..299_999 ms).
    store.on_trade("BTC", 100.0, 0.1, 1_000)
    store.on_trade("BTC", 110.0, 0.2, 100_000)
    store.on_trade("BTC", 95.0, 0.05, 200_000)
    store.on_trade("BTC", 105.0, 0.15, 299_999)

    df = store.get_candles("BTC")
    assert len(df) == 1
    row = df.iloc[0]
    assert row.open == 100.0  # first trade
    assert row.high == 110.0
    assert row.low == 95.0
    assert row.close == 105.0  # last trade
    assert row.volume == pytest.approx(0.5)


def test_trades_crossing_bar_boundary_create_two_bars() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    # Bar 0: 0..299_999
    store.on_trade("BTC", 100.0, 0.1, 1_000)
    store.on_trade("BTC", 110.0, 0.2, 200_000)
    # Bar 1: 300_000..599_999
    store.on_trade("BTC", 105.0, 0.1, 300_001)
    store.on_trade("BTC", 120.0, 0.2, 500_000)

    df = store.get_candles("BTC")
    assert len(df) == 2
    bar0, bar1 = df.iloc[0], df.iloc[1]

    assert bar0.open == 100.0
    assert bar0.high == 110.0
    assert bar0.low == 100.0
    assert bar0.close == 110.0
    assert bar0.volume == pytest.approx(0.3)

    assert bar1.open == 105.0
    assert bar1.high == 120.0
    assert bar1.low == 105.0
    assert bar1.close == 120.0
    assert bar1.volume == pytest.approx(0.3)

    # Index values are the bar open times.
    assert df.index[0] == pd.Timestamp(0, unit="ms", tz="UTC")
    assert df.index[1] == pd.Timestamp(BAR_MS, unit="ms", tz="UTC")


def test_gap_between_trades_does_not_fabricate_intermediate_bars() -> None:
    """If no trades in bar 1, we don't synthesise an empty bar — only real bars."""
    store = CandleStore(history=10, bar_seconds=300)
    store.on_trade("BTC", 100.0, 0.1, 1_000)  # bar 0
    store.on_trade("BTC", 200.0, 0.2, 700_000)  # bar 2 (skipping bar 1)

    df = store.get_candles("BTC")
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp(0, unit="ms", tz="UTC")
    assert df.index[1] == pd.Timestamp(2 * BAR_MS, unit="ms", tz="UTC")


def test_out_of_order_trade_for_past_bar_is_ignored() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    store.on_trade("BTC", 100.0, 0.1, 100_000)  # bar 0
    store.on_trade("BTC", 200.0, 0.5, 400_000)  # bar 1
    store.on_trade("BTC", 50.0, 0.3, 50_000)  # late trade for bar 0 — ignored

    df = store.get_candles("BTC")
    assert len(df) == 2
    bar0 = df.iloc[0]
    # Bar 0's low is unchanged; the late 50.0 trade did NOT mutate it.
    assert bar0.low == 100.0
    assert bar0.volume == pytest.approx(0.1)


def test_history_caps_the_number_of_bars_kept() -> None:
    store = CandleStore(history=3, bar_seconds=300)
    # Five distinct bars; only the last 3 should remain.
    for i in range(5):
        store.on_trade("BTC", 100.0 + i, 0.1, i * BAR_MS + 1_000)

    df = store.get_candles("BTC")
    assert len(df) == 3
    assert df.iloc[0].open == 102.0  # bar 2
    assert df.iloc[-1].open == 104.0  # bar 4


def test_multiple_symbols_have_independent_state() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    store.on_trade("BTC", 100.0, 0.1, 1_000)
    store.on_trade("ETH", 2000.0, 1.0, 1_000)
    store.on_trade("BTC", 110.0, 0.2, 200_000)

    btc = store.get_candles("BTC")
    eth = store.get_candles("ETH")
    assert len(btc) == 1
    assert len(eth) == 1
    assert btc.iloc[0].close == 110.0
    assert eth.iloc[0].close == 2000.0
    assert set(store.symbols()) == {"BTC", "ETH"}


def test_on_trades_message_dispatches_each_trade() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    payload = [
        {"coin": "BTC", "px": "100.0", "sz": "0.1", "time": 1_000, "side": "B"},
        {"coin": "BTC", "px": "110.0", "sz": "0.2", "time": 200_000, "side": "A"},
        {"coin": "ETH", "px": "2000.0", "sz": "1.0", "time": 200_000, "side": "B"},
    ]
    store.on_trades_message(payload)

    btc = store.get_candles("BTC")
    eth = store.get_candles("ETH")
    assert len(btc) == 1
    assert btc.iloc[0].open == 100.0
    assert btc.iloc[0].close == 110.0
    assert btc.iloc[0].volume == pytest.approx(0.3)
    assert len(eth) == 1
    assert eth.iloc[0].close == 2000.0


def test_get_candles_for_unknown_symbol_returns_empty_dataframe() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    df = store.get_candles("NOPE")
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None
    assert str(df.index.tz) == "UTC"
    assert df.index.name == "time"


def test_index_is_utc_timezone_aware() -> None:
    store = CandleStore(history=10, bar_seconds=300)
    store.on_trade("BTC", 100.0, 0.1, 1_700_000_000_000)
    df = store.get_candles("BTC")
    assert str(df.index.tz) == "UTC"


def test_invalid_constructor_args_raise() -> None:
    with pytest.raises(ValueError):
        CandleStore(history=0, bar_seconds=300)
    with pytest.raises(ValueError):
        CandleStore(history=10, bar_seconds=0)


def test_bar_seconds_property_exposed() -> None:
    store = CandleStore(history=10, bar_seconds=60)
    assert store.bar_seconds == 60


def test_60_second_bars_bucket_correctly() -> None:
    """Confirm bar boundaries scale with bar_seconds, not hardcoded to 300."""
    store = CandleStore(history=10, bar_seconds=60)
    store.on_trade("BTC", 100.0, 0.1, 30_000)  # bar 0 (0..59_999)
    store.on_trade("BTC", 110.0, 0.1, 70_000)  # bar 1 (60_000..119_999)
    df = store.get_candles("BTC")
    assert len(df) == 2
    assert df.index[0] == pd.Timestamp(0, unit="ms", tz="UTC")
    assert df.index[1] == pd.Timestamp(60_000, unit="ms", tz="UTC")
