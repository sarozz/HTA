"""Strategy A — intraday mean reversion. Pure functions only.

`evaluate` returns the desired post-bar TargetPosition for a symbol given
its candle history and current position. It is deterministic and free of
I/O — the order router decides how to bridge from the current position
to the target, and the risk manager gates whether the resulting order
intent is allowed.

Spec (SPEC.md, Strategy A):
  Entry  : LONG  when z_score < -2.0 AND rsi < 30
           SHORT when z_score > +2.0 AND rsi > 70
  Exits  : target  when |z_score| crosses 0
           stop    when |z_score| > 3.5
           time    when bars_held > 4
  Sizing : risk_dollars = 0.5% * equity
           notional     = risk_dollars / stop_distance_pct
           cap          at 30% of equity
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from src.marketdata.features import rsi, z_score


@dataclass(frozen=True)
class StrategyPosition:
    """Position state the strategy needs from the caller (main loop)."""

    symbol: str
    size: Decimal  # signed: positive long, negative short, 0 = flat
    entry_price: Decimal
    bars_held: int  # 0 means just opened; incremented on each closed bar


@dataclass(frozen=True)
class TargetPosition:
    """Desired position after this bar."""

    symbol: str
    notional: Decimal  # signed; 0 means flat
    reason: str


@dataclass(frozen=True)
class MeanReversionConfig:
    sma_window: int = 96
    rsi_window: int = 14
    z_entry: Decimal = Decimal("2.0")
    z_target: Decimal = Decimal("0.0")
    z_stop: Decimal = Decimal("3.5")
    rsi_oversold: Decimal = Decimal("30")
    rsi_overbought: Decimal = Decimal("70")
    time_stop_bars: int = 4
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_notional_pct: Decimal = Decimal("0.30")


def _to_decimal(x: object) -> Decimal | None:
    """Convert numpy/pandas scalar to Decimal, returning None on NaN/missing."""
    if x is None:
        return None
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return Decimal(str(f))


def _flat(symbol: str, reason: str) -> TargetPosition:
    return TargetPosition(symbol=symbol, notional=Decimal("0"), reason=reason)


def _hold(position: StrategyPosition, last_close: Decimal, reason: str) -> TargetPosition:
    return TargetPosition(
        symbol=position.symbol,
        notional=position.size * last_close,
        reason=reason,
    )


def _compute_size(
    equity: Decimal,
    last_close: Decimal,
    sigma: Decimal,
    config: MeanReversionConfig,
) -> Decimal:
    """Position notional per SPEC: risk_dollars / stop_distance_pct, capped.

    stop_distance is the price move from entry (|z|=z_entry) to stop
    (|z|=z_stop), expressed as a fraction of last_close. Both expressed in
    units of sigma (the rolling stdev).
    """
    if sigma <= 0 or last_close <= 0 or equity <= 0:
        return Decimal("0")
    risk_dollars = config.risk_per_trade_pct * equity
    stop_distance = (config.z_stop - config.z_entry) * sigma
    if stop_distance <= 0:
        return Decimal("0")
    stop_distance_pct = stop_distance / last_close
    if stop_distance_pct <= 0:
        return Decimal("0")
    notional = risk_dollars / stop_distance_pct
    cap = config.max_notional_pct * equity
    if notional > cap:
        notional = cap
    return notional


def evaluate(
    symbol: str,
    candles: pd.DataFrame,
    position: StrategyPosition | None,
    equity: Decimal,
    config: MeanReversionConfig | None = None,
) -> TargetPosition:
    """Return the target position for `symbol` given the latest candles.

    `candles` must contain a 'close' column indexed by bar open time. The
    function reads the most recent values; everything is lagged inside
    `z_score` and `rsi` so feature[t] uses only data <= bar t-1, which
    means we can call `evaluate` at bar t's close without leakage.
    """
    cfg = config or MeanReversionConfig()

    if "close" not in candles.columns:
        return _flat(symbol, "no close column")
    closes = candles["close"]
    if len(closes) < cfg.sma_window + 1:
        return _flat(symbol, "insufficient history")

    z_series = z_score(closes, cfg.sma_window)
    r_series = rsi(closes, cfg.rsi_window)
    sigma_series = closes.rolling(window=cfg.sma_window, min_periods=cfg.sma_window).std(ddof=0)

    z = _to_decimal(z_series.iloc[-1])
    r = _to_decimal(r_series.iloc[-1])
    sigma = _to_decimal(sigma_series.iloc[-2])  # match the lag of z_series
    last_close = _to_decimal(closes.iloc[-1])

    if z is None or r is None or sigma is None or last_close is None:
        return _flat(symbol, "feature warmup")

    holding = position is not None and position.size != 0

    # ---------------- Exit logic (highest priority) ------------------------
    if holding:
        # Stop: |z| > z_stop
        if abs(z) > cfg.z_stop:
            return _flat(symbol, f"stop |z|={abs(z):.3f}")

        # Time stop: bars_held > N
        if position.bars_held > cfg.time_stop_bars:
            return _flat(symbol, f"time_stop bars_held={position.bars_held}")

        # Target: |z| crosses 0 (sign changed vs. position direction).
        # Long: was negative z, now z >= z_target (0). Short: opposite.
        if position.size > 0 and z >= cfg.z_target:
            return _flat(symbol, f"target z={z:.3f}")
        if position.size < 0 and z <= -cfg.z_target:
            return _flat(symbol, f"target z={z:.3f}")

        return _hold(position, last_close, f"hold z={z:.3f} bars={position.bars_held}")

    # ---------------- Entry logic ------------------------------------------
    if z < -cfg.z_entry and r < cfg.rsi_oversold:
        size_notional = _compute_size(equity, last_close, sigma, cfg)
        if size_notional <= 0:
            return _flat(symbol, "size=0; skip entry")
        return TargetPosition(
            symbol=symbol,
            notional=size_notional,
            reason=f"long z={z:.3f} rsi={r:.2f}",
        )

    if z > cfg.z_entry and r > cfg.rsi_overbought:
        size_notional = _compute_size(equity, last_close, sigma, cfg)
        if size_notional <= 0:
            return _flat(symbol, "size=0; skip entry")
        return TargetPosition(
            symbol=symbol,
            notional=-size_notional,
            reason=f"short z={z:.3f} rsi={r:.2f}",
        )

    return _flat(symbol, f"no signal z={z:.3f} rsi={r:.2f}")
