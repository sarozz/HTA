"""Run the Strategy B funding-capture backtest against real Hyperliquid data.

Pulls 180 days of /info fundingHistory for BTC, ETH, SOL (testnet by
default) and runs the simulator. Produces:

  reports/strategy_b_backtest.md
  reports/strategy_b_equity_curve.png

Cannot run in the dev sandbox (Hyperliquid hosts are on the egress
denylist). Run on a machine with network access:

    python -m scripts.run_funding_backtest

Override the network with --mainnet only if you understand the
implications. Default and recommended is testnet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.funding_data_loader import (
    DEFAULT_CACHE_DIR,
    fetch_funding_history_for_coins,
)
from src.backtest.funding_report import write_report
from src.backtest.funding_simulator import FundingSimConfig, simulate

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_URL = "https://api.hyperliquid.xyz"
COINS = ["BTC", "ETH", "SOL"]
DAYS = 180


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mainnet",
        action="store_true",
        help="Use mainnet instead of testnet (read-only data; no orders).",
    )
    args = parser.parse_args()
    base_url = MAINNET_URL if args.mainnet else TESTNET_URL
    network = "mainnet" if args.mainnet else "testnet"
    print(f"using {network} at {base_url}")

    # Lazy import so the module is importable without the SDK installed.
    from hyperliquid.info import Info

    info = Info(base_url, skip_ws=True)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS)
    print(f"window: [{start.isoformat()}, {end.isoformat()})  coins={COINS}")

    df = fetch_funding_history_for_coins(
        info=info,
        coins=COINS,
        start=start,
        end=end,
        cache_dir=REPO_ROOT / DEFAULT_CACHE_DIR,
    )
    print(f"fetched {len(df)} funding ticks across {df['coin'].nunique()} coins")

    cfg = FundingSimConfig()  # SPEC defaults
    result = simulate(df, cfg)

    report_path = REPO_ROOT / "reports" / "strategy_b_backtest.md"
    chart_path = REPO_ROOT / "reports" / "strategy_b_equity_curve.png"
    write_report(
        result,
        output_path=report_path,
        chart_path=chart_path,
        title=f"Strategy B backtest — {DAYS}-day {network}",
        intro=(
            f"Real {network} funding history. Fees and fill stochastics are "
            f"modelled per SPEC.md. The funding rate IS the signal — i.e. the "
            f"backtest assumes perfect prediction of the realised rate, which "
            f"is optimistic but stable in Hyperliquid's regime."
        ),
        run_details={
            "network": network,
            "coins": ",".join(COINS),
            "days": DAYS,
            "tick_count": len(df),
            "data_source": "Hyperliquid /info fundingHistory",
        },
    )
    print(f"wrote {report_path}")
    print(f"wrote {chart_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
