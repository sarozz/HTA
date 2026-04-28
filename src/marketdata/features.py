"""Pure feature functions used by Strategy A.

Both `z_score` and `rsi` are LAGGED so that feature[t] uses only data
available at the previous bar's close — i.e., a strategy reading
feature[t] at the open of bar t cannot see the close of bar t.

This is achieved by computing the indicator on the full closes series
and then `.shift(1)`. The simulator can therefore consume features
without further offsetting and remain look-ahead-free by construction.
"""

from __future__ import annotations

import pandas as pd


def z_score(closes: pd.Series, window: int) -> pd.Series:
    """Population z-score of `closes` over `window`, lagged by one bar.

    z[t] = (closes[t-1] - SMA(closes[t-window:t-1])) /
           STD(closes[t-window:t-1], ddof=0)

    Returns NaN until enough history is available (and one extra bar for
    the lag). Uses population standard deviation (ddof=0).
    """
    if window <= 1:
        raise ValueError("window must be >= 2")
    sma = closes.rolling(window=window, min_periods=window).mean()
    std = closes.rolling(window=window, min_periods=window).std(ddof=0)
    raw = (closes - sma) / std
    return raw.shift(1)


def rsi(closes: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI over `period`, lagged by one bar.

    Uses Wilder's smoothing (EWMA with alpha = 1/period). Returns NaN
    until at least `period` deltas are available (and one extra bar
    for the lag).
    """
    if period < 2:
        raise ValueError("period must be >= 2")
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    raw = 100.0 - 100.0 / (1.0 + rs)
    # If avg_loss is zero, rs is +inf and rsi is 100. If both zero, NaN.
    raw = raw.where(~(avg_loss == 0), 100.0)
    raw = raw.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return raw.shift(1)
