"""Markdown report for Strategy B backtest results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.backtest.funding_simulator import (
    FundingSimResult,
    FundingSimStats,
    aggregate,
    attempts_to_dataframe,
)


def _bps(x: float) -> str:
    return f"{x * 10_000:.2f} bps"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _format_stats(stats: FundingSimStats) -> str:
    pnl_pct = (
        (stats.final_equity - stats.initial_equity) / stats.initial_equity
        if stats.initial_equity > 0
        else 0.0
    )
    rows = [
        ("Total ticks observed", str(stats.total_ticks)),
        ("Skipped (below threshold)", str(stats.skipped)),
        ("Attempted captures", str(stats.attempted)),
        ("Succeeded (paired fill)", str(stats.succeeded)),
        ("Failed (unhedged → unwound)", str(stats.failed)),
        (
            "Win rate",
            f"{stats.win_rate * 100:.2f}%" if stats.attempted > 0 else "n/a",
        ),
        ("Funding received (gross)", _money(stats.funding_received)),
        ("Fees paid", _money(stats.fees_paid)),
        ("Slippage paid", _money(stats.slippage_paid)),
        ("Total P&L", _money(stats.total_pnl)),
        ("Initial equity", _money(stats.initial_equity)),
        ("Final equity", _money(stats.final_equity)),
        ("Equity return", _pct(pnl_pct)),
    ]
    out = ["| Metric | Value |", "| --- | --- |"]
    for k, v in rows:
        out.append(f"| {k} | {v} |")
    return "\n".join(out)


def _format_config(cfg: Any) -> str:
    rows = [
        ("Funding threshold", _pct(cfg.funding_threshold)),
        ("Pre-tick entry (s)", str(cfg.pre_tick_seconds)),
        ("Fill window (s)", str(cfg.fill_window_seconds)),
        ("Post-tick exit min (s)", str(cfg.post_tick_min_seconds)),
        ("Post-tick exit max (s)", str(cfg.post_tick_max_seconds)),
        ("Maker fee per side", _bps(cfg.maker_fee_per_side)),
        ("Taker fee (unwind)", _bps(cfg.taker_fee)),
        ("Slippage per leg", _bps(cfg.slippage_per_leg)),
        ("Paired-fill probability", _pct(cfg.paired_fill_probability)),
        ("Notional per leg", _pct(cfg.notional_per_leg_pct)),
        ("Initial equity", _money(cfg.initial_equity)),
        ("RNG seed", str(cfg.rng_seed)),
    ]
    out = ["| Parameter | Value |", "| --- | --- |"]
    for k, v in rows:
        out.append(f"| {k} | {v} |")
    return "\n".join(out)


def _format_per_coin(result: FundingSimResult) -> str:
    df = attempts_to_dataframe(result.attempts)
    if df.empty:
        return "(no attempts)"
    grouped = df.groupby("coin").agg(
        attempts=("outcome", lambda s: int((s != "skipped").sum())),
        succeeded=("outcome", lambda s: int((s == "success").sum())),
        failed=("outcome", lambda s: int((s == "fill_failure").sum())),
        funding_received=("funding_pnl", "sum"),
        fees=("fees", "sum"),
        slippage=("slippage", "sum"),
        net_pnl=("net_pnl", "sum"),
    )
    out = [
        "| Coin | Attempts | Won | Lost | Funding | Fees | Slippage | Net P&L |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for coin, row in grouped.iterrows():
        out.append(
            f"| {coin} | {int(row.attempts)} | {int(row.succeeded)} | "
            f"{int(row.failed)} | {_money(row.funding_received)} | "
            f"{_money(row.fees)} | {_money(row.slippage)} | {_money(row.net_pnl)} |"
        )
    return "\n".join(out)


def _format_attempts_table(result: FundingSimResult, max_rows: int = 200) -> str:
    """Show only attempted captures (success + fill_failure) chronologically.

    Skipped ticks are far more numerous and not actionable; the summary
    block already gives the count.
    """
    df = attempts_to_dataframe(result.attempts)
    if df.empty:
        return "(no attempts)"
    df = df[df["outcome"] != "skipped"].copy()
    if df.empty:
        return "(no captures attempted — all ticks below threshold)"
    df["tick_time"] = df["tick_time"].apply(
        lambda t: t.strftime("%Y-%m-%d %H:%M") if t is not None else ""
    )
    df["funding_rate"] = df["funding_rate"].apply(_pct)
    for col in ("notional", "funding_pnl", "fees", "slippage", "net_pnl"):
        df[col] = df[col].apply(_money)
    truncated = len(df) > max_rows
    df = df.head(max_rows)

    out = [
        "| Tick (UTC) | Coin | Funding | Notional | Outcome | Fills | "
        "Funding $ | Fees | Slippage | Net P&L | Note |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in df.itertuples(index=False):
        out.append(
            f"| {r.tick_time} | {r.coin} | {r.funding_rate} | {r.notional} | "
            f"{r.outcome} | {r.fills} | {r.funding_pnl} | {r.fees} | "
            f"{r.slippage} | {r.net_pnl} | {r.note} |"
        )
    if truncated:
        out.append(
            f"\n*Showing first {max_rows} attempted captures of "
            f"{len(df) + (len(df) > max_rows) * (max_rows - len(df))} attempts.*"
        )
    return "\n".join(out)


def write_equity_curve(result: FundingSimResult, png_path: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(result.equity_curve, color="#2563eb", linewidth=1.5)
    ax.set_xlabel("tick index (chronological)")
    ax.set_ylabel("equity ($)")
    ax.set_title("Strategy B funding-capture equity curve")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)


def write_report(
    result: FundingSimResult,
    *,
    output_path: Path,
    chart_path: Path | None = None,
    title: str = "Strategy B backtest",
    intro: str | None = None,
    run_details: dict[str, Any] | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if chart_path is not None:
        write_equity_curve(result, chart_path)
    stats = aggregate(result)

    pieces: list[str] = [f"# {title}"]
    if intro:
        pieces.append(f"\n> {intro}\n")
    if chart_path is not None:
        pieces.append(f"\n![equity curve]({chart_path.name})\n")
    pieces.append("## Summary\n")
    pieces.append(_format_stats(stats))
    pieces.append("\n## Configuration\n")
    pieces.append(_format_config(result.config))
    pieces.append("\n## Per-coin breakdown\n")
    pieces.append(_format_per_coin(result))
    pieces.append("\n## Capture attempts\n")
    pieces.append(_format_attempts_table(result))

    pieces.append("\n## Run details\n")
    details = {"generated_at": datetime.now(timezone.utc).isoformat()}
    if run_details:
        details.update(run_details)
    for k, v in details.items():
        pieces.append(f"- **{k}**: {v}")

    output_path.write_text("\n".join(pieces) + "\n")
