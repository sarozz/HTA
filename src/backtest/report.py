"""Backtest report writer.

Renders a SimulationResult as a markdown file with:
  - Headline metrics: Sharpe, max drawdown, win rate, profit factor,
    avg trade, total trades, fees paid, slippage paid
  - Per-symbol breakdown
  - Embedded PNG of the equity curve

Sharpe is computed on PER-BAR equity returns (5-minute bars by default)
and annualised assuming 365 days/year of 24/7 crypto trading. We use
the simple formula: sqrt(N_per_year) * mean(returns) / std(returns).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless rendering, no display required
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.backtest.simulator import SimulationResult

# 5-minute bars × 12 per hour × 24 × 365 ≈ 105,120
DEFAULT_PERIODS_PER_YEAR = 365 * 24 * 12


@dataclass(frozen=True)
class Metrics:
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_trade_pnl: float
    sharpe: float
    max_drawdown: float
    total_pnl: float
    fees_paid: float
    slippage_paid: float
    skipped_signals: int


def _compute_metrics(
    result: SimulationResult, periods_per_year: int = DEFAULT_PERIODS_PER_YEAR
) -> Metrics:
    trades = result.trades
    equity = result.equity_curve

    total_trades = len(trades)
    if total_trades == 0:
        sharpe = 0.0
        mdd = 0.0
        win_rate = 0.0
        pf = 0.0
        avg = 0.0
        total = 0.0
        fees = 0.0
        slip = 0.0
    else:
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] < 0]
        win_rate = len(wins) / total_trades if total_trades else 0.0
        gross_profit = wins["pnl"].sum() if not wins.empty else 0.0
        gross_loss = -losses["pnl"].sum() if not losses.empty else 0.0
        pf = float(gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        avg = float(trades["pnl"].mean())
        total = float(trades["pnl"].sum())
        fees = float(trades["fees"].sum())
        slip = float(trades["slippage_cost"].abs().sum())

        # Sharpe on per-bar equity returns.
        if not equity.empty:
            rets = equity.pct_change().dropna()
            if rets.std() > 0:
                sharpe = float(np.sqrt(periods_per_year) * rets.mean() / rets.std())
            else:
                sharpe = 0.0
            running_peak = equity.cummax()
            drawdown = (equity - running_peak) / running_peak
            mdd = float(drawdown.min())
        else:
            sharpe = 0.0
            mdd = 0.0

    return Metrics(
        total_trades=total_trades,
        win_rate=win_rate,
        profit_factor=pf,
        avg_trade_pnl=avg,
        sharpe=sharpe,
        max_drawdown=mdd,
        total_pnl=total,
        fees_paid=fees,
        slippage_paid=slip,
        skipped_signals=result.skipped_signals,
    )


def _per_symbol_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["trades", "wins", "win_rate", "total_pnl", "avg_pnl", "fees"])
    grouped = trades.groupby("symbol").agg(
        trades=("pnl", "size"),
        wins=("pnl", lambda s: int((s > 0).sum())),
        total_pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
        fees=("fees", "sum"),
    )
    grouped["win_rate"] = grouped["wins"] / grouped["trades"]
    return grouped[["trades", "wins", "win_rate", "total_pnl", "avg_pnl", "fees"]]


def _exit_reason_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["count", "total_pnl", "avg_pnl"])
    return trades.groupby("exit_reason").agg(
        count=("pnl", "size"),
        total_pnl=("pnl", "sum"),
        avg_pnl=("pnl", "mean"),
    )


def _render_equity_curve(equity: pd.Series, png_path: Path, title: str) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    if equity.empty:
        ax.text(0.5, 0.5, "no trades", ha="center", va="center", transform=ax.transAxes)
    else:
        ax.plot(equity.index, equity.values, linewidth=1.0, color="#2563eb")
        ax.set_xlabel("time (UTC)")
        ax.set_ylabel("equity (USD)")
        ax.grid(True, alpha=0.3)
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)


def write_report(
    result: SimulationResult,
    *,
    output_md: Path | str,
    chart_filename: str = "equity_curve.png",
    title: str = "Strategy A backtest",
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the markdown report and PNG chart, return the markdown path."""
    output_md = Path(output_md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    chart_path = output_md.parent / chart_filename
    _render_equity_curve(result.equity_curve, chart_path, title)

    metrics = _compute_metrics(result)
    by_symbol = _per_symbol_breakdown(result.trades)
    by_reason = _exit_reason_breakdown(result.trades)
    cfg = result.strategy_config
    sim = result.sim_config

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if note:
        lines.append(f"> **Note**: {note}")
        lines.append("")

    lines.append(f"![equity curve]({chart_filename})")
    lines.append("")

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total trades | {metrics.total_trades} |")
    lines.append(f"| Win rate | {metrics.win_rate:.2%} |")
    pf = f"{metrics.profit_factor:.2f}" if np.isfinite(metrics.profit_factor) else "inf"
    lines.append(f"| Profit factor | {pf} |")
    lines.append(f"| Avg trade P&L | ${metrics.avg_trade_pnl:,.2f} |")
    lines.append(f"| Sharpe (annualised) | {metrics.sharpe:.2f} |")
    lines.append(f"| Max drawdown | {metrics.max_drawdown:.2%} |")
    lines.append(f"| Total P&L | ${metrics.total_pnl:,.2f} |")
    lines.append(f"| Fees paid | ${metrics.fees_paid:,.2f} |")
    lines.append(f"| Slippage paid | ${metrics.slippage_paid:,.2f} |")
    lines.append(f"| Skipped signals (ALO no-fill) | {metrics.skipped_signals} |")
    lines.append("")

    lines.append("## Configuration")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Initial equity | ${sim.initial_equity:,.2f} |")
    lines.append(f"| z entry | {cfg.z_entry} |")
    lines.append(f"| z stop | {cfg.z_stop} |")
    lines.append(f"| z target | {cfg.z_target} |")
    lines.append(f"| Time stop (bars) | {cfg.time_stop_bars} |")
    lines.append(f"| RSI window | {cfg.rsi_window} |")
    lines.append(f"| RSI oversold/overbought | {cfg.rsi_oversold} / {cfg.rsi_overbought} |")
    lines.append(f"| SMA window | {cfg.sma_window} |")
    lines.append(f"| Risk per trade | {cfg.risk_per_trade_pct:.2%} |")
    lines.append(f"| Max notional | {cfg.max_notional_pct:.0%} of equity |")
    lines.append(f"| Maker fee | {sim.maker_fee_rate:.4%} per side |")
    lines.append(f"| Slippage | {sim.slippage_bps} bps per fill |")
    lines.append(f"| ALO fill probability | {sim.fill_prob:.0%} |")
    lines.append(f"| RNG seed | {sim.seed} |")
    lines.append("")

    lines.append("## Per-symbol breakdown")
    lines.append("")
    if by_symbol.empty:
        lines.append("_no trades_")
    else:
        lines.append("| Symbol | Trades | Wins | Win rate | Total P&L | Avg P&L | Fees |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for sym, row in by_symbol.iterrows():
            lines.append(
                f"| {sym} | {int(row.trades)} | {int(row.wins)} | "
                f"{row.win_rate:.2%} | ${row.total_pnl:,.2f} | "
                f"${row.avg_pnl:,.2f} | ${row.fees:,.2f} |"
            )
    lines.append("")

    lines.append("## Exit reason breakdown")
    lines.append("")
    if by_reason.empty:
        lines.append("_no trades_")
    else:
        lines.append("| Reason | Count | Total P&L | Avg P&L |")
        lines.append("| --- | ---: | ---: | ---: |")
        for reason, row in by_reason.iterrows():
            lines.append(
                f"| {reason} | {int(row['count'])} | "
                f"${row.total_pnl:,.2f} | ${row.avg_pnl:,.2f} |"
            )
    lines.append("")

    if extra:
        lines.append("## Run details")
        lines.append("")
        for k, v in extra.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")

    output_md.write_text("\n".join(lines) + "\n")
    return output_md
