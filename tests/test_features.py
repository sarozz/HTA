"""Tests for src/marketdata/features.py.

Each indicator is verified against hand-computed values, and a look-ahead
invariance test confirms that changing closes[k] cannot affect feature[i]
for i <= k.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.marketdata.features import rsi, z_score

# ---------------------------------------------------------------------------
# z_score
# ---------------------------------------------------------------------------


def test_z_score_known_values_window_3() -> None:
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    z = z_score(closes, window=3)

    # raw[t]: NaN, NaN, 1.2247, 1.2247, 1.2247
    # shifted by 1: NaN, NaN, NaN, 1.2247, 1.2247
    expected = math.sqrt(3 / 2)  # 1 / sqrt(2/3)
    assert math.isnan(z.iloc[0])
    assert math.isnan(z.iloc[1])
    assert math.isnan(z.iloc[2])
    assert math.isclose(z.iloc[3], expected, rel_tol=1e-9)
    assert math.isclose(z.iloc[4], expected, rel_tol=1e-9)


def test_z_score_constant_series_yields_nan_or_inf() -> None:
    # Constant series: std = 0, raw = 0/0 = NaN.
    closes = pd.Series([5.0] * 6)
    z = z_score(closes, window=3)
    # All values are NaN (either pre-window or 0/0).
    assert z.isna().all()


def test_z_score_rejects_invalid_window() -> None:
    with pytest.raises(ValueError):
        z_score(pd.Series([1.0, 2.0]), window=1)


def test_z_score_is_lagged_no_look_ahead() -> None:
    rng = np.random.default_rng(seed=42)
    closes_a = pd.Series(rng.normal(100.0, 1.0, size=20))
    closes_b = closes_a.copy()
    closes_b.iloc[10] = closes_a.iloc[10] + 50.0  # large change at index 10

    z_a = z_score(closes_a, window=4)
    z_b = z_score(closes_b, window=4)

    # z[i] uses closes[i-window-1 .. i-1]. For i <= 10, closes[..i-1] is
    # untouched, so values must match (NaN where applicable).
    for i in range(11):
        if pd.isna(z_a.iloc[i]):
            assert pd.isna(z_b.iloc[i]), i
        else:
            assert math.isclose(z_a.iloc[i], z_b.iloc[i], rel_tol=1e-12), i

    # z[11] depends on closes[10] -> must differ.
    assert not math.isclose(z_a.iloc[11], z_b.iloc[11], rel_tol=1e-6)


# ---------------------------------------------------------------------------
# rsi
# ---------------------------------------------------------------------------


def test_rsi_strict_uptrend_is_100() -> None:
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    r = rsi(closes, period=2)

    # raw[0..1]: NaN; raw[2..5] = 100. After shift(1): rsi[3..5] = 100.
    assert pd.isna(r.iloc[0])
    assert pd.isna(r.iloc[1])
    assert pd.isna(r.iloc[2])
    assert math.isclose(r.iloc[3], 100.0)
    assert math.isclose(r.iloc[4], 100.0)
    assert math.isclose(r.iloc[5], 100.0)


def test_rsi_strict_downtrend_is_0() -> None:
    closes = pd.Series([15.0, 14.0, 13.0, 12.0, 11.0, 10.0])
    r = rsi(closes, period=2)
    assert math.isclose(r.iloc[3], 0.0)
    assert math.isclose(r.iloc[4], 0.0)
    assert math.isclose(r.iloc[5], 0.0)


def test_rsi_constant_series_is_50() -> None:
    closes = pd.Series([100.0] * 6)
    r = rsi(closes, period=2)
    # Both avg_gain and avg_loss are 0 once they propagate, so we map to 50.
    assert math.isclose(r.iloc[3], 50.0)


def test_rsi_known_oscillation() -> None:
    # Up 1, down 1, up 1, down 1, ... with period=2 (alpha=0.5).
    # delta:    [NaN, 1, -1, 1, -1, 1]
    # gain:     [NaN, 1,  0, 1,  0, 1]
    # loss:     [NaN, 0,  1, 0,  1, 0]
    # ewm(alpha=0.5, adjust=False, min_periods=2):
    #   avg_gain: [NaN, NaN, 0.5, 0.75, 0.375, 0.6875]
    #   avg_loss: [NaN, NaN, 0.5, 0.25, 0.625, 0.3125]
    #   rs (idx>=2): 1, 3, 0.6, 2.2
    #   raw rsi   : 50, 75, 37.5, 68.75
    # After shift(1) the values land at indices 3, 4, 5, 6.
    closes = pd.Series([10.0, 11.0, 10.0, 11.0, 10.0, 11.0])
    r = rsi(closes, period=2)
    assert math.isclose(r.iloc[3], 50.0, rel_tol=1e-9)
    assert math.isclose(r.iloc[4], 75.0, rel_tol=1e-9)
    assert math.isclose(r.iloc[5], 37.5, rel_tol=1e-9)


def test_rsi_rejects_invalid_period() -> None:
    with pytest.raises(ValueError):
        rsi(pd.Series([1.0, 2.0]), period=1)


def test_rsi_is_lagged_no_look_ahead() -> None:
    rng = np.random.default_rng(seed=1)
    closes_a = pd.Series(100.0 + rng.normal(0, 1, size=30).cumsum())
    closes_b = closes_a.copy()
    closes_b.iloc[15] = closes_a.iloc[15] + 100.0

    r_a = rsi(closes_a, period=4)
    r_b = rsi(closes_b, period=4)

    for i in range(16):
        if pd.isna(r_a.iloc[i]):
            assert pd.isna(r_b.iloc[i]), i
        else:
            assert math.isclose(r_a.iloc[i], r_b.iloc[i], rel_tol=1e-12), i

    assert not math.isclose(r_a.iloc[16], r_b.iloc[16], rel_tol=1e-6)
