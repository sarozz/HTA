"""Tests for src/backtest/report.py.

Verifies that:
  - the markdown report is written with all required sections
  - the PNG chart file is produced and non-empty
  - headline metrics are computed correctly on a hand-built result
  - empty results don't crash the renderer
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.report import _compute_metrics, write_report
from src.backtest.simulator import (
    TRADE_COLUMNS,
    SimulationResult,
    SimulatorConfig,
    StrategyConfig,
)


def _make_result(trades: list[dict], equity: pd.Series, skipped: int = 0) -> SimulationResult:
    df = (
        pd.DataFrame(trades, columns=list(TRADE_COLUMNS))
        if trades
        else pd.DataFrame(columns=list(TRADE_COLUMNS))
    )
    return SimulationResult(
        trades=df,
        equity_curve=equity,
        strategy_config=StrategyConfig(),
        sim_config=SimulatorConfig(),
        skipped_signals=skipped,
    )


def test_metrics_for_known_trades() -> None:
    idx = pd.date_range("2026-01-01", periods=5, freq="5min", tz="UTC")
    trades = [
        {
            "symbol": "BTC",
            "side": "long",
            "entry_time": idx[0],
            "exit_time": idx[1],
            "entry_price": 100.0,
            "exit_price": 110.0,
            "size": 1.0,
            "notional": 100.0,
            "fees": 0.03,
            "slippage_cost": 0.01,
            "pnl": 9.97,
            "exit_reason": "target",
            "bars_held": 1,
        },
        {
            "symbol": "BTC",
            "side": "long",
            "entry_time": idx[2],
            "exit_time": idx[3],
            "entry_price": 100.0,
            "exit_price": 95.0,
            "size": 1.0,
            "notional": 100.0,
            "fees": 0.03,
            "slippage_cost": 0.01,
            "pnl": -5.03,
            "exit_reason": "stop",
            "bars_held": 1,
        },
    ]
    # Equity timeline: 1500, 1509.97, 1509.97, 1504.94, 1504.94
    eq = pd.Series([1500.0, 1509.97, 1509.97, 1504.94, 1504.94], index=idx, name="equity")
    result = _make_result(trades, eq, skipped=3)

    m = _compute_metrics(result)
    assert m.total_trades == 2
    assert m.win_rate == 0.5
    # PF = gross profit / gross loss = 9.97 / 5.03
    assert abs(m.profit_factor - (9.97 / 5.03)) < 1e-9
    assert abs(m.avg_trade_pnl - (9.97 - 5.03) / 2) < 1e-9
    assert abs(m.total_pnl - (9.97 - 5.03)) < 1e-9
    assert abs(m.fees_paid - 0.06) < 1e-9
    assert abs(m.slippage_paid - 0.02) < 1e-9
    assert m.skipped_signals == 3
    # Max drawdown is the trough from peak 1509.97 to 1504.94.
    expected_mdd = (1504.94 - 1509.97) / 1509.97
    assert abs(m.max_drawdown - expected_mdd) < 1e-9


def test_metrics_for_empty_result() -> None:
    eq = pd.Series(dtype="float64", name="equity")
    result = _make_result([], eq)
    m = _compute_metrics(result)
    assert m.total_trades == 0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.sharpe == 0.0
    assert m.max_drawdown == 0.0


def test_metrics_all_winners_gives_inf_profit_factor() -> None:
    idx = pd.date_range("2026-01-01", periods=2, freq="5min", tz="UTC")
    trades = [
        {
            "symbol": "BTC",
            "side": "long",
            "entry_time": idx[0],
            "exit_time": idx[1],
            "entry_price": 100.0,
            "exit_price": 110.0,
            "size": 1.0,
            "notional": 100.0,
            "fees": 0.0,
            "slippage_cost": 0.0,
            "pnl": 10.0,
            "exit_reason": "target",
            "bars_held": 1,
        }
    ]
    eq = pd.Series([1500.0, 1510.0], index=idx, name="equity")
    m = _compute_metrics(_make_result(trades, eq))
    assert m.profit_factor == float("inf")


def test_write_report_produces_md_and_png(tmp_path: Path) -> None:
    idx = pd.date_range("2026-01-01", periods=3, freq="5min", tz="UTC")
    trades = [
        {
            "symbol": "BTC",
            "side": "long",
            "entry_time": idx[0],
            "exit_time": idx[1],
            "entry_price": 100.0,
            "exit_price": 110.0,
            "size": 1.0,
            "notional": 100.0,
            "fees": 0.03,
            "slippage_cost": 0.01,
            "pnl": 9.97,
            "exit_reason": "target",
            "bars_held": 1,
        },
        {
            "symbol": "ETH",
            "side": "short",
            "entry_time": idx[1],
            "exit_time": idx[2],
            "entry_price": 2000.0,
            "exit_price": 1990.0,
            "size": 0.05,
            "notional": 100.0,
            "fees": 0.03,
            "slippage_cost": 0.01,
            "pnl": 0.47,
            "exit_reason": "target",
            "bars_held": 1,
        },
    ]
    eq = pd.Series([1500.0, 1509.97, 1510.44], index=idx, name="equity")
    result = _make_result(trades, eq, skipped=2)

    md_path = tmp_path / "report.md"
    out = write_report(
        result,
        output_md=md_path,
        chart_filename="curve.png",
        title="Test backtest",
        note="synthetic data",
        extra={"data_source": "synthetic", "bars": 100},
    )
    assert out == md_path
    assert md_path.exists()
    body = md_path.read_text()
    # Required sections.
    for needle in (
        "# Test backtest",
        "synthetic data",
        "## Headline metrics",
        "## Configuration",
        "## Per-symbol breakdown",
        "## Exit reason breakdown",
        "## Run details",
        "![equity curve](curve.png)",
        "BTC",
        "ETH",
        "data_source",
    ):
        assert needle in body, f"missing: {needle!r}"

    png = tmp_path / "curve.png"
    assert png.exists()
    assert png.stat().st_size > 1000  # non-trivial PNG


def test_write_report_handles_empty_result(tmp_path: Path) -> None:
    eq = pd.Series(dtype="float64", name="equity")
    result = _make_result([], eq)
    md = tmp_path / "report.md"
    write_report(result, output_md=md, chart_filename="empty.png")
    assert md.exists()
    body = md.read_text()
    assert "_no trades_" in body
    assert (tmp_path / "empty.png").exists()


def test_sharpe_zero_for_constant_equity_curve() -> None:
    idx = pd.date_range("2026-01-01", periods=10, freq="5min", tz="UTC")
    eq = pd.Series([1500.0] * 10, index=idx, name="equity")
    trades = [
        {
            "symbol": "BTC",
            "side": "long",
            "entry_time": idx[0],
            "exit_time": idx[1],
            "entry_price": 100.0,
            "exit_price": 100.0,
            "size": 1.0,
            "notional": 100.0,
            "fees": 0.0,
            "slippage_cost": 0.0,
            "pnl": 0.0,
            "exit_reason": "target",
            "bars_held": 1,
        }
    ]
    m = _compute_metrics(_make_result(trades, eq))
    # Zero std on returns -> sharpe defined as 0 (rather than NaN/inf).
    assert m.sharpe == 0.0
    assert np.isfinite(m.sharpe)
