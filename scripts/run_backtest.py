"""Run the Strategy A backtest against real Hyperliquid candle data.

Default behaviour: pull 180 days of 5-minute candles for BTC, ETH, SOL
from mainnet (read-only — candleSnapshot is unauthenticated; no orders
are placed) and write a markdown report to reports/strategy_a_backtest.md.

The data loader caches each chunk to data/cache/<coin>_5m_<start>_<end>.parquet
so re-runs only fetch missing windows.

Usage:
    python -m scripts.run_backtest                # 180 days, 3 symbols
    python -m scripts.run_backtest --days 90      # shorter window
    python -m scripts.run_backtest --coins BTC    # single symbol

Notes:
  * candleSnapshot is a read-only /info endpoint. We pull from MAINNET
    here on purpose: testnet's candle history is sparse and may not
    represent realistic conditions. CLAUDE.md non-negotiable #1
    governs ORDER URLs (always testnet for orders); historical data
    pulls are read-only and unauthenticated, so this script uses
    mainnet for data only and never touches the order path.
  * Console output reports each chunk: which symbol, which date range,
    cache hit or miss, and how many bars were stored — so you can see
    the loader working in real time.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants

from src.backtest.data_loader import ProgressEvent, load_candles
from src.backtest.report import write_report
from src.backtest.simulator import (
    SimulatorConfig,
    StrategyConfig,
    run_simulation,
)

DEFAULT_COINS = ("BTC", "ETH", "SOL")
DEFAULT_DAYS = 180
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "reports" / "strategy_a_backtest.md"


def _on_progress(event: ProgressEvent) -> None:
    start = datetime.fromtimestamp(event.chunk_start_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )
    end = datetime.fromtimestamp(event.chunk_end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    tag = "cache" if event.cache_hit else "fetch"
    print(
        f"  [{tag}] {event.coin} {start} → {end} "
        f"(chunk {event.chunk_index + 1}/{event.chunk_count}, {event.bars} bars)"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument(
        "--coins",
        type=str,
        nargs="+",
        default=list(DEFAULT_COINS),
        help="Hyperliquid coin symbols (e.g. BTC ETH SOL)",
    )
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    p.add_argument(
        "--mainnet",
        action="store_true",
        default=True,
        help="Pull historical data from mainnet (default; read-only).",
    )
    args = p.parse_args(argv)

    base_url = constants.MAINNET_API_URL if args.mainnet else constants.TESTNET_API_URL
    print(f"connecting to {base_url} (read-only candleSnapshot only)...")
    info = Info(base_url, skip_ws=True)

    print(f"loading {args.days} days of 5m candles for {args.coins}...")
    t0 = time.time()
    candles = load_candles(
        coins=list(args.coins),
        days=args.days,
        info=info,
        cache_dir=args.cache_dir,
        on_progress=_on_progress,
    )
    elapsed = time.time() - t0

    total_bars = sum(len(df) for df in candles.values())
    print(f"loaded {total_bars:,} bars across {len(candles)} symbols " f"in {elapsed:.1f}s")
    for coin, df in candles.items():
        if df.empty:
            print(f"  {coin}: NO DATA")
        else:
            print(f"  {coin}: {len(df):,} bars  {df.index[0]} → {df.index[-1]}")

    print("running simulator (Strategy A, defaults from SPEC.md/RISK.md)...")
    cfg = StrategyConfig()
    sim_cfg = SimulatorConfig()
    result = run_simulation(candles, cfg, sim_cfg)
    print(
        f"  trades: {len(result.trades)}  "
        f"skipped: {result.skipped_signals}  "
        f"final equity: ${result.equity_curve.iloc[-1]:,.2f}"
        if not result.equity_curve.empty
        else "  no bars / no trades"
    )

    print(f"writing report to {args.report}...")
    write_report(
        result,
        output_md=args.report,
        chart_filename="strategy_a_equity_curve.png",
        title=f"Strategy A backtest — {args.days} days, {', '.join(args.coins)}",
        note=(
            "Real Hyperliquid candleSnapshot data. "
            f"Run on {datetime.now(timezone.utc).isoformat()}."
        ),
        extra={
            "data_source": "Hyperliquid mainnet /info candleSnapshot (read-only)",
            "days": args.days,
            "coins": ", ".join(args.coins),
            "total_bars": total_bars,
            "cache_dir": str(args.cache_dir),
        },
    )
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
