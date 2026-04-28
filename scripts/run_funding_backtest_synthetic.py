"""Strategy B synthetic-data demo.

Generates a deterministic 60-day synthetic funding history with
characteristics resembling Hyperliquid (mostly small positive funding,
occasional spikes), runs the simulator, and writes a full report. Used
to exercise the simulator end-to-end in the sandbox where real
Hyperliquid data is not reachable.

The real run is in scripts/run_funding_backtest.py.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.funding_report import write_report
from src.backtest.funding_simulator import FundingSimConfig, simulate

REPO_ROOT = Path(__file__).resolve().parent.parent

DAYS = 60
COINS = ["BTC", "ETH", "SOL"]
RNG_SEED = 20260428


def _generate_funding_history(days: int, coins: list[str], seed: int) -> pd.DataFrame:
    """Per coin, hourly ticks with small positive median + occasional spikes.

    Hyperliquid funding on majors typically sits at 0-3 bps per hour with
    occasional spikes to 10-30 bps. We model:
      - log-normal-ish base: median ~1 bp, occasional 5-15 bp values
      - planted spikes: a handful of >0.10% (10 bp) ticks per coin to
        guarantee the simulator exercises both success and failure paths
    """
    rng = np.random.default_rng(seed)
    start = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    n_hours = days * 24
    rows: list[dict] = []
    for coin in coins:
        for h in range(n_hours):
            tick_time = start + timedelta(hours=h)
            # Base: half-normal scaled to ~1 bp median.
            base = abs(rng.normal(0, 1.5)) / 10_000  # ~0-0.05% with most below 0.02%
            rows.append({"tick_time": tick_time, "coin": coin, "funding_rate": base})

    df = pd.DataFrame(rows).sort_values(["tick_time", "coin"]).reset_index(drop=True)

    # Plant some captures: scatter ~30 spikes per coin above the 0.10% gate.
    for coin in coins:
        coin_idx = df[df["coin"] == coin].index.tolist()
        chosen = rng.choice(coin_idx, size=30, replace=False)
        for idx in chosen:
            spike = float(rng.uniform(0.0011, 0.0030))  # 11-30 bps
            df.at[idx, "funding_rate"] = spike
    return df


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    print(f"generating {DAYS}-day synthetic funding history (seed={RNG_SEED})...")
    df = _generate_funding_history(DAYS, COINS, RNG_SEED)
    print(f"  {len(df)} ticks; " f"{(df['funding_rate'] > 0.001).sum()} above 0.10% threshold")

    cfg = FundingSimConfig()
    result = simulate(df, cfg)

    report_path = REPO_ROOT / "reports" / "strategy_b_backtest.md"
    chart_path = REPO_ROOT / "reports" / "strategy_b_equity_curve.png"
    write_report(
        result,
        output_path=report_path,
        chart_path=chart_path,
        title=f"Strategy B backtest — {DAYS}-day SYNTHETIC demo",
        intro=(
            "**This run uses a synthetic funding history, not real Hyperliquid "
            "data.** It exists to demonstrate the simulator end-to-end and to "
            "exercise both the paired-fill success path and the unhedged-leg "
            "failure path. The real 180-day run is "
            "`scripts/run_funding_backtest.py` and overwrites this file."
        ),
        run_details={
            "data_source": "synthetic (deterministic, seed below)",
            "rng_seed": RNG_SEED,
            "days": DAYS,
            "coins": ",".join(COINS),
            "tick_count": len(df),
            "planted_spikes_per_coin": 30,
        },
    )
    print(f"wrote {report_path}")
    print(f"wrote {chart_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
