"""Run the Strategy A backtest against a deterministic synthetic dataset.

This is the in-sandbox demo. The real 180-day run lives in
scripts/run_backtest.py and pulls Hyperliquid /info candleSnapshot,
which is not reachable from this sandbox.

Synthetic generator:
  - 60 days × 3 symbols of 5m bars
  - Background: zero-drift Gaussian log-returns calibrated so the
    rolling 96-bar std is roughly stable
  - Two engineered events on BTC, planted exactly where the rolling
    SMA/STD are stable enough that the injected delta produces the
    desired z-score:
      * Event A: a 2.5-sigma DROP that reverts in 3 bars
        (expected: LONG entry → target exit)
      * Event B: a 4-sigma DROP that keeps going
        (expected: LONG entry → stop-out)

Output: reports/strategy_a_backtest.md with embedded equity curve PNG.
The report is explicit that the data is synthetic.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.report import write_report
from src.backtest.simulator import (
    SimulatorConfig,
    StrategyConfig,
    compute_signals,
    run_simulation,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = PROJECT_ROOT / "reports" / "strategy_a_backtest.md"

DAYS = 60
BARS_PER_DAY = 24 * 12
COINS = ("BTC", "ETH", "SOL")
BASE_PRICES = {"BTC": 60_000.0, "ETH": 3_000.0, "SOL": 100.0}
PER_BAR_VOL = 0.0008  # ~8 bps log-return stdev per 5m bar
SEED = 20260428


def _ohlc_from_closes(closes: np.ndarray, opens: np.ndarray) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes),
            "low": np.minimum(opens, closes),
            "close": closes,
            "volume": np.full(n, 1.0),
        }
    )


def _inject_event(
    closes: np.ndarray,
    bar: int,
    *,
    direction: int,
    sigma_entry: float,
    follow_through: list[float],
) -> None:
    """Mutate `closes` to plant a calibrated event at `bar`.

    `direction` is -1 for a downside move (long setup) or +1 for upside.
    `sigma_entry` is the magnitude of the entry-bar move in std units;
    after lagging, the simulator sees z = direction * sigma_entry at
    bar+1 and the entry signal fires there.

    `follow_through` is a list of SIGNED z-scores: at bar+offset, the
    close is set to `sma + sigma * std`. Use NEGATIVE values to keep
    the price below mean (continuation) and POSITIVE values to revert
    above mean (target exit).

    Stats are computed on `closes` BEFORE mutation so the planted
    z-score is what the simulator's rolling window will measure.
    """
    window = 96
    if bar < window + 4:
        raise ValueError(f"bar {bar} too early for stable {window}-bar window")
    series = pd.Series(closes)
    sma = series.rolling(window, min_periods=window).mean().iloc[bar - 1]
    std = series.rolling(window, min_periods=window).std(ddof=0).iloc[bar - 1]
    if not np.isfinite(sma) or not np.isfinite(std) or std <= 0:
        raise RuntimeError("rolling stats not finite at injection point")

    # Pre-event: a small drift in the same direction across the prior 3
    # bars so RSI(14) tilts towards the entry side. Sufficient on its
    # own to push RSI < 30 (or > 70 for shorts) once the big move lands.
    for k in (3, 2, 1):
        closes[bar - k] = sma + direction * 0.4 * std * (4 - k) / 3.0

    # Entry-trigger close: z_raw[bar] = direction * sigma_entry, so the
    # simulator sees z[bar+1] = direction * sigma_entry.
    closes[bar] = sma + direction * sigma_entry * std

    # Follow-through closes: signed z-scores relative to (sma, std).
    for offset, sigma in enumerate(follow_through, start=1):
        closes[bar + offset] = sma + sigma * std


def generate_synthetic_market(
    seed: int, days: int, coins: tuple[str, ...]
) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    n = days * BARS_PER_DAY
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    out: dict[str, pd.DataFrame] = {}

    for coin in coins:
        base_price = BASE_PRICES[coin]
        log_returns = rng.normal(0.0, PER_BAR_VOL, size=n)
        log_returns[0] = 0.0
        closes = base_price * np.exp(np.cumsum(log_returns))

        if coin == "BTC":
            # Plant the two events at well-separated bars where the rolling
            # 96-bar window is fully populated and stable.
            event_a = n // 4  # ~day 15
            event_b = 3 * n // 4  # ~day 45

            # Event A: 2.5σ down, then revert past the mean within 3 bars.
            # Entry triggers at event_a+1 (z = -2.5). Target fires when z
            # crosses 0, i.e. when a follow-through close >= sma; we put
            # that at event_a+3 with z = +0.5.
            _inject_event(
                closes,
                event_a,
                direction=-1,
                sigma_entry=2.5,
                follow_through=[-1.5, -0.5, 0.5, 1.0],
            )
            # Event B: 2.5σ down (LONG entry), then continued slide deeper
            # than -3.5σ (stop-out).
            _inject_event(
                closes,
                event_b,
                direction=-1,
                sigma_entry=2.5,
                follow_through=[-3.5, -4.0, -4.2, -4.0],
            )

        # Treat each bar's open as the previous bar's close (no-gap model).
        opens = np.concatenate([[closes[0]], closes[:-1]])
        df = _ohlc_from_closes(closes, opens).set_index(idx)
        df.index.name = "time"
        out[coin] = df

    return out


def main() -> int:
    print(f"generating {DAYS}-day synthetic market for {COINS}...")
    candles = generate_synthetic_market(SEED, DAYS, COINS)
    total_bars = sum(len(df) for df in candles.values())
    print(f"  {total_bars:,} bars total")

    # Quick sanity: how many entry signals does the strategy fire?
    cfg = StrategyConfig()
    for coin, df in candles.items():
        sigs = compute_signals(df, cfg)
        print(
            f"  {coin}: {int(sigs['entry_long'].sum())} long entries, "
            f"{int(sigs['entry_short'].sum())} short entries"
        )

    sim_cfg = SimulatorConfig()
    result = run_simulation(candles, cfg, sim_cfg)
    print(
        f"trades: {len(result.trades)}, "
        f"skipped (ALO no-fill): {result.skipped_signals}, "
        f"final equity: ${result.equity_curve.iloc[-1]:,.2f}"
    )
    if not result.trades.empty:
        by_reason = result.trades["exit_reason"].value_counts().to_dict()
        print(f"  exit reasons: {by_reason}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_report(
        result,
        output_md=REPORT_PATH,
        chart_filename="strategy_a_equity_curve.png",
        title=f"Strategy A backtest — {DAYS}-day SYNTHETIC demo",
        note=(
            "**This run uses a synthetic dataset, not real Hyperliquid data.** "
            "It exists to demonstrate the backtester end-to-end and to exercise "
            "both a target-exit and a stop-out. The real 180-day backtest is "
            "run via `scripts/run_backtest.py` on a machine with network access "
            "to Hyperliquid; the report path is the same so re-running on "
            "real data overwrites this file."
        ),
        extra={
            "data_source": "synthetic (deterministic, seed below)",
            "rng_seed": SEED,
            "days": DAYS,
            "coins": ", ".join(COINS),
            "per_bar_log_return_std": PER_BAR_VOL,
            "planted_events": (
                "BTC bar n/4: 2.5σ drop reverting in ~3 bars (target). "
                "BTC bar 3n/4: 2.5σ drop with continued 4σ slide (stop-out)."
            ),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"wrote report to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
