"""Vectorised Strategy A backtester.

Implements SPEC.md "Strategy A — Intraday Mean Reversion" exactly:
  Entry  : LONG when z[t] <= -z_entry AND rsi[t] <= rsi_oversold
           SHORT when z[t] >= +z_entry AND rsi[t] >= rsi_overbought
  Exit   : target  — |z| crosses 0
           stop    — |z| > z_stop
           time    — bars_held > time_stop_bars
  Size   : notional = min(risk_dollars / stop_distance_pct,
                          max_notional_pct * equity)
           where stop_distance_pct = 1.5 * std / entry_close

Models (as per the session brief):
  - 0.015% maker fee per side
  - 70% of ALO orders fill; the other 30% are skipped (no entry)
  - 0.5 bps slippage per fill (long buys higher, sells lower; short sells
    lower at entry, buys higher at exit)

Look-ahead-free by construction: features are lagged via features.py
(feature[t] uses only data through bar t-1) and execution happens at
the OPEN of the same bar t. The simulator never reads close[t] before
deciding to act on bar t.

Sizing simplification: the spec's per-trade equity feed-forward is
approximated by sizing every trade off `initial_equity`. We document
this rather than hide it. Trades' P&L is summed (additive), which is
what reaches the equity curve.

The simulator is "vectorised" in that all per-bar quantities (features,
SMAs, signal masks) are computed via pandas; the trade lifecycle is
walked in a single pass per symbol (entries are sparse; the loop is
O(n) but the per-bar work is O(1)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.marketdata.features import rsi, z_score

# ---------------------------------------------------------------------------
# Configs and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyConfig:
    sma_window: int = 96
    rsi_window: int = 14
    z_entry: float = 2.0
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    z_target: float = 0.0
    z_stop: float = 3.5
    time_stop_bars: int = 4
    risk_per_trade_pct: float = 0.005
    max_notional_pct: float = 0.30
    # Stop distance in price terms is approximated as
    # stop_distance_multiplier * std_at_entry, i.e. the price move from
    # the entry z-score to the |z| = z_stop level. With z_entry = 2.0
    # and z_stop = 3.5 the implied multiplier is 1.5.
    stop_distance_multiplier: float = 1.5


@dataclass(frozen=True)
class SimulatorConfig:
    initial_equity: float = 1500.0
    maker_fee_rate: float = 0.00015  # 0.015% per side
    slippage_bps: float = 0.5  # 0.00005 of price per fill
    fill_prob: float = 0.70  # 70% of ALO entries fill
    seed: int = 42


# Per-trade record, materialised as a row of the trades DataFrame.
TRADE_COLUMNS = (
    "symbol",
    "side",  # "long" | "short"
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "size",
    "notional",
    "fees",
    "slippage_cost",
    "pnl",
    "exit_reason",  # "target" | "stop" | "time_stop"
    "bars_held",
)


@dataclass(frozen=True)
class SimulationResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    strategy_config: StrategyConfig
    sim_config: SimulatorConfig
    skipped_signals: int = 0

    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Signal computation (vectorised, pure)
# ---------------------------------------------------------------------------


def compute_signals(candles: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Return a DataFrame aligned to `candles` with feature and signal columns.

    Output columns:
      z              — z-score, lagged (feature[t] uses through bar t-1)
      r              — RSI, lagged
      sma            — rolling mean used by z, lagged
      std            — rolling std (population) used by z, lagged
      entry_long     — bool; LONG entry signal at bar t (act at open[t])
      entry_short    — bool; SHORT entry signal at bar t
      exit_target_long  — bool; LONG target exit at bar t
      exit_stop_long    — bool; LONG stop exit at bar t
      exit_target_short — bool; SHORT target exit at bar t
      exit_stop_short   — bool; SHORT stop exit at bar t
    """
    closes = candles["close"]
    z = z_score(closes, cfg.sma_window)
    r = rsi(closes, cfg.rsi_window)
    sma = closes.rolling(cfg.sma_window, min_periods=cfg.sma_window).mean().shift(1)
    std = closes.rolling(cfg.sma_window, min_periods=cfg.sma_window).std(ddof=0).shift(1)

    entry_long = (z <= -cfg.z_entry) & (r <= cfg.rsi_oversold)
    entry_short = (z >= cfg.z_entry) & (r >= cfg.rsi_overbought)
    # Target exits: |z| has crossed back to 0 (or beyond, away from entry side).
    exit_target_long = z >= cfg.z_target
    exit_target_short = z <= -cfg.z_target  # z_target is normally 0
    # Stop exits: |z| moved further from the mean than z_stop.
    exit_stop_long = z <= -cfg.z_stop
    exit_stop_short = z >= cfg.z_stop

    return pd.DataFrame(
        {
            "z": z,
            "r": r,
            "sma": sma,
            "std": std,
            "entry_long": entry_long.fillna(False),
            "entry_short": entry_short.fillna(False),
            "exit_target_long": exit_target_long.fillna(False),
            "exit_target_short": exit_target_short.fillna(False),
            "exit_stop_long": exit_stop_long.fillna(False),
            "exit_stop_short": exit_stop_short.fillna(False),
        },
        index=candles.index,
    )


# ---------------------------------------------------------------------------
# Per-symbol simulation
# ---------------------------------------------------------------------------


def _size_position(
    entry_close: float,
    std_at_entry: float,
    cfg: StrategyConfig,
    sim_cfg: SimulatorConfig,
) -> tuple[float, float, float]:
    """Return (notional_usd, size_base, stop_distance_pct).

    stop_distance_pct uses entry_close as the reference price. If std is
    zero or NaN, we cannot size; returns (0, 0, 0) and the caller skips.
    """
    if not np.isfinite(std_at_entry) or std_at_entry <= 0 or entry_close <= 0:
        return 0.0, 0.0, 0.0
    stop_distance_pct = cfg.stop_distance_multiplier * std_at_entry / entry_close
    if stop_distance_pct <= 0:
        return 0.0, 0.0, 0.0
    risk_dollars = cfg.risk_per_trade_pct * sim_cfg.initial_equity
    raw_notional = risk_dollars / stop_distance_pct
    cap = cfg.max_notional_pct * sim_cfg.initial_equity
    notional = min(raw_notional, cap)
    size = notional / entry_close
    return notional, size, stop_distance_pct


def _simulate_symbol(
    symbol: str,
    candles: pd.DataFrame,
    signals: pd.DataFrame,
    cfg: StrategyConfig,
    sim_cfg: SimulatorConfig,
    rng: np.random.Generator,
) -> tuple[list[dict[str, Any]], int]:
    """Run a single symbol's strategy A and return (trades, skipped_count).

    Walks bars in order. Maintains at most one open position. Entries fire
    at OPEN[t] when feature[t] crosses an entry threshold; exits fire at
    OPEN[t] when feature[t] crosses target/stop or the time stop expires.
    On a single bar, exits take precedence over entries.
    """
    trades: list[dict[str, Any]] = []
    skipped = 0

    opens = candles["open"].to_numpy(dtype=float)
    times = candles.index
    std = signals["std"].to_numpy(dtype=float)
    entry_long = signals["entry_long"].to_numpy(dtype=bool)
    entry_short = signals["entry_short"].to_numpy(dtype=bool)
    exit_target_long = signals["exit_target_long"].to_numpy(dtype=bool)
    exit_target_short = signals["exit_target_short"].to_numpy(dtype=bool)
    exit_stop_long = signals["exit_stop_long"].to_numpy(dtype=bool)
    exit_stop_short = signals["exit_stop_short"].to_numpy(dtype=bool)

    slippage = sim_cfg.slippage_bps / 10_000.0  # 0.5 bps -> 0.00005
    fee_rate = sim_cfg.maker_fee_rate

    in_position = False
    side: str = ""
    entry_idx = -1
    entry_fill: float = 0.0
    size: float = 0.0
    notional: float = 0.0

    n = len(candles)
    for t in range(n):
        # --- exit check first -------------------------------------------
        if in_position:
            bars_held = t - entry_idx
            should_exit = False
            exit_reason = ""

            if side == "long":
                if exit_stop_long[t]:
                    should_exit, exit_reason = True, "stop"
                elif exit_target_long[t]:
                    should_exit, exit_reason = True, "target"
                elif bars_held > cfg.time_stop_bars:
                    should_exit, exit_reason = True, "time_stop"
            else:  # short
                if exit_stop_short[t]:
                    should_exit, exit_reason = True, "stop"
                elif exit_target_short[t]:
                    should_exit, exit_reason = True, "target"
                elif bars_held > cfg.time_stop_bars:
                    should_exit, exit_reason = True, "time_stop"

            if should_exit:
                raw_exit = opens[t]
                if not np.isfinite(raw_exit):
                    raw_exit = entry_fill
                # Slippage: long sells lower, short buys higher.
                exit_fill = (
                    raw_exit * (1.0 - slippage) if side == "long" else raw_exit * (1.0 + slippage)
                )
                exit_fee = abs(size) * exit_fill * fee_rate

                if side == "long":
                    gross = (exit_fill - entry_fill) * size
                else:
                    gross = (entry_fill - exit_fill) * size
                entry_fee = notional * fee_rate
                slippage_cost = (
                    abs(size) * (raw_exit - exit_fill) * (1.0 if side == "long" else -1.0)
                )
                # Slippage at entry, signed: entry_fill differs from raw open.
                # Track total slippage in dollars (positive = cost).
                pnl = gross - entry_fee - exit_fee
                trades.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "entry_time": times[entry_idx],
                        "exit_time": times[t],
                        "entry_price": entry_fill,
                        "exit_price": exit_fill,
                        "size": size,
                        "notional": notional,
                        "fees": entry_fee + exit_fee,
                        "slippage_cost": slippage_cost,
                        "pnl": pnl,
                        "exit_reason": exit_reason,
                        "bars_held": bars_held,
                    }
                )
                in_position = False
                side = ""
                entry_idx = -1
                entry_fill = 0.0
                size = 0.0
                notional = 0.0
                # Don't enter on the same bar we exit on.
                continue

        # --- entry check ------------------------------------------------
        if not in_position and (entry_long[t] or entry_short[t]):
            wants = "long" if entry_long[t] else "short"
            raw_open = opens[t]
            if not np.isfinite(raw_open) or raw_open <= 0:
                continue
            # Sample fill (70% by default).
            if rng.random() >= sim_cfg.fill_prob:
                skipped += 1
                continue
            std_t = std[t]
            notional_n, size_n, _ = _size_position(raw_open, std_t, cfg, sim_cfg)
            if size_n <= 0:
                skipped += 1
                continue
            # Entry slippage: long buys higher, short sells lower.
            entry_fill = (
                raw_open * (1.0 + slippage) if wants == "long" else raw_open * (1.0 - slippage)
            )
            in_position = True
            side = wants
            entry_idx = t
            size = size_n
            notional = notional_n

    return trades, skipped


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run_simulation(
    candles_by_symbol: dict[str, pd.DataFrame],
    cfg: StrategyConfig | None = None,
    sim_cfg: SimulatorConfig | None = None,
) -> SimulationResult:
    cfg = cfg or StrategyConfig()
    sim_cfg = sim_cfg or SimulatorConfig()
    rng = np.random.default_rng(sim_cfg.seed)

    all_trades: list[dict[str, Any]] = []
    skipped_total = 0
    union_index = pd.DatetimeIndex([], tz="UTC")

    for symbol, candles in candles_by_symbol.items():
        if candles.empty:
            continue
        signals = compute_signals(candles, cfg)
        trades, skipped = _simulate_symbol(symbol, candles, signals, cfg, sim_cfg, rng)
        all_trades.extend(trades)
        skipped_total += skipped
        union_index = union_index.union(candles.index)

    trades_df = (
        pd.DataFrame(all_trades, columns=list(TRADE_COLUMNS))
        if all_trades
        else pd.DataFrame(columns=list(TRADE_COLUMNS))
    )
    if not trades_df.empty:
        trades_df = trades_df.sort_values("exit_time").reset_index(drop=True)

    # Equity curve: stamp each trade's pnl at its exit time over the union
    # of all symbols' bar timestamps; cumulative-sum gives equity.
    if union_index.empty:
        equity = pd.Series(dtype="float64", name="equity")
    else:
        equity = pd.Series(0.0, index=union_index, name="equity").sort_index()
        if not trades_df.empty:
            grouped = trades_df.groupby("exit_time")["pnl"].sum()
            # Reindex onto the equity timeline; missing -> 0.
            equity = equity.add(grouped.reindex(equity.index, fill_value=0.0), fill_value=0.0)
        equity = sim_cfg.initial_equity + equity.cumsum()

    return SimulationResult(
        trades=trades_df,
        equity_curve=equity,
        strategy_config=cfg,
        sim_config=sim_cfg,
        skipped_signals=skipped_total,
    )
