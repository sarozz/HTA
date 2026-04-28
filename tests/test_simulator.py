"""Tests for src/backtest/simulator.py.

Two layers of tests:
  1. **Mechanics**: drive `_simulate_symbol` with hand-built candles +
     pre-computed signals so each test isolates one piece of behaviour
     (entry timing, exit precedence, sizing, fees, slippage, fill
     probability). Every value here is hand-checked.
  2. **Integration**: run the full pipeline (compute_signals +
     simulator) on a 30-bar synthetic series with a single planted
     LONG setup. Exercises the look-ahead invariance and verifies the
     number of generated trades.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.backtest.simulator import (
    SimulatorConfig,
    StrategyConfig,
    _simulate_symbol,
    compute_signals,
    run_simulation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars(n: int, open_prices: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    opens = open_prices if open_prices is not None else [100.0] * n
    closes = list(opens)
    return pd.DataFrame(
        {
            "open": opens,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * n,
        },
        index=idx,
    )


def _empty_signals(idx: pd.DatetimeIndex, std_value: float = 5.0) -> pd.DataFrame:
    """Signal frame with all flags False; std/z populated with a default."""
    n = len(idx)
    return pd.DataFrame(
        {
            "z": [0.0] * n,
            "r": [50.0] * n,
            "sma": [100.0] * n,
            "std": [std_value] * n,
            "entry_long": [False] * n,
            "entry_short": [False] * n,
            "exit_target_long": [False] * n,
            "exit_target_short": [False] * n,
            "exit_stop_long": [False] * n,
            "exit_stop_short": [False] * n,
        },
        index=idx,
    )


def _zero_cost_sim_cfg(seed: int = 0) -> SimulatorConfig:
    """Sim config with no fees, no slippage, deterministic 100% fill."""
    return SimulatorConfig(
        initial_equity=1500.0,
        maker_fee_rate=0.0,
        slippage_bps=0.0,
        fill_prob=1.0,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# Mechanics: long entry/exit
# ---------------------------------------------------------------------------


def test_long_target_exit_pnl_with_zero_costs() -> None:
    """LONG entered at bar 3 (open=100), target exit at bar 6 (open=110)."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(10, open_prices=[100, 100, 100, 100, 105, 108, 110, 110, 110, 110])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[6], "exit_target_long"] = True

    rng = np.random.default_rng(0)
    trades, skipped = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, rng)

    assert skipped == 0
    assert len(trades) == 1
    t = trades[0]
    # Sizing: stop_distance_pct = 1.5 * 5 / 100 = 0.075;
    # raw_notional = 7.5 / 0.075 = 100 (well under the 450 cap).
    assert math.isclose(t["notional"], 100.0)
    assert math.isclose(t["size"], 1.0)  # 100 / 100
    assert t["entry_price"] == 100.0  # zero slippage
    assert t["exit_price"] == 110.0
    assert math.isclose(t["pnl"], 10.0)  # (110-100)*1 - 0 fees - 0 slippage
    assert t["exit_reason"] == "target"
    assert t["bars_held"] == 3
    assert t["side"] == "long"


def test_long_stop_takes_precedence_over_target() -> None:
    """Both stop and target flags set; stop wins."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(8, open_prices=[100] * 4 + [90] + [100] * 3)
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[4], "exit_target_long"] = True
    signals.loc[candles.index[4], "exit_stop_long"] = True

    rng = np.random.default_rng(0)
    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, rng)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop"
    assert math.isclose(trades[0]["pnl"], -10.0)  # (90-100)*1 with size=1


def test_long_time_stop_when_no_target_or_stop_fires() -> None:
    """No target/stop flags -> time_stop_bars triggers exit at bar entry+5."""
    cfg = StrategyConfig(time_stop_bars=4)
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(15, open_prices=[100.0 + i for i in range(15)])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True

    rng = np.random.default_rng(0)
    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, rng)
    assert len(trades) == 1
    t = trades[0]
    # Held > 4 bars -> exit at bar 3+5 = 8. open[8] = 108.
    assert t["exit_reason"] == "time_stop"
    assert t["bars_held"] == 5
    assert t["entry_price"] == 103.0
    assert t["exit_price"] == 108.0


# ---------------------------------------------------------------------------
# Mechanics: short side
# ---------------------------------------------------------------------------


def test_short_target_exit_pnl_with_zero_costs() -> None:
    """SHORT entered at 100, target exit at 90 -> +10 P&L per unit."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(8, open_prices=[100, 100, 100, 100, 95, 92, 90, 90])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_short"] = True
    signals.loc[candles.index[6], "exit_target_short"] = True

    rng = np.random.default_rng(0)
    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, rng)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "short"
    assert t["entry_price"] == 100.0
    assert t["exit_price"] == 90.0
    assert math.isclose(t["pnl"], 10.0)


def test_short_stop_exit_loss() -> None:
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(6, open_prices=[100, 100, 100, 100, 110, 110])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_short"] = True
    signals.loc[candles.index[4], "exit_stop_short"] = True

    rng = np.random.default_rng(0)
    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, rng)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop"
    assert math.isclose(trades[0]["pnl"], -10.0)


# ---------------------------------------------------------------------------
# Fees and slippage accounting
# ---------------------------------------------------------------------------


def test_fees_charged_on_both_sides() -> None:
    cfg = StrategyConfig()
    sim_cfg = SimulatorConfig(
        initial_equity=1500.0,
        maker_fee_rate=0.00015,
        slippage_bps=0.0,
        fill_prob=1.0,
        seed=0,
    )
    candles = _bars(8, open_prices=[100, 100, 100, 100, 100, 100, 110, 110])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[6], "exit_target_long"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    assert len(trades) == 1
    t = trades[0]
    # Entry fee = notional * 0.00015 = 100 * 0.00015 = 0.015
    # Exit fee = size * exit_fill * 0.00015 = 1 * 110 * 0.00015 = 0.0165
    expected_fees = 0.015 + 0.0165
    assert math.isclose(t["fees"], expected_fees, rel_tol=1e-9)
    # PnL gross = (110-100)*1 = 10; after fees = 10 - expected_fees
    assert math.isclose(t["pnl"], 10.0 - expected_fees, rel_tol=1e-9)


def test_slippage_long_buys_higher_sells_lower() -> None:
    cfg = StrategyConfig()
    sim_cfg = SimulatorConfig(
        initial_equity=1500.0,
        maker_fee_rate=0.0,
        slippage_bps=10.0,  # 10 bps = 0.001
        fill_prob=1.0,
        seed=0,
    )
    candles = _bars(8, open_prices=[100, 100, 100, 100, 100, 100, 110, 110])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[6], "exit_target_long"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    assert len(trades) == 1
    t = trades[0]
    # Entry fill = 100 * 1.001 = 100.1; exit fill = 110 * 0.999 = 109.89
    assert math.isclose(t["entry_price"], 100.1, rel_tol=1e-9)
    assert math.isclose(t["exit_price"], 109.89, rel_tol=1e-9)


def test_slippage_short_sells_lower_buys_higher() -> None:
    cfg = StrategyConfig()
    sim_cfg = SimulatorConfig(
        initial_equity=1500.0,
        maker_fee_rate=0.0,
        slippage_bps=10.0,
        fill_prob=1.0,
        seed=0,
    )
    candles = _bars(8, open_prices=[100, 100, 100, 100, 100, 100, 90, 90])
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_short"] = True
    signals.loc[candles.index[6], "exit_target_short"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    assert len(trades) == 1
    t = trades[0]
    # SHORT entry: sell lower -> 100*0.999 = 99.9. Exit: buy higher -> 90*1.001 = 90.09
    assert math.isclose(t["entry_price"], 99.9, rel_tol=1e-9)
    assert math.isclose(t["exit_price"], 90.09, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def test_position_sizing_uncapped_uses_risk_over_stop_distance() -> None:
    """notional = (0.5% * 1500) / (1.5 * std / open_at_entry) when below cap."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(6, open_prices=[100] * 3 + [200] + [200] * 2)
    # Use a large std so notional stays below the 30% cap.
    # stop_distance_pct = 1.5 * 30 / 200 = 0.225
    # raw_notional = 7.5 / 0.225 = 33.33
    signals = _empty_signals(candles.index, std_value=30.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[4], "exit_target_long"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    t = trades[0]
    assert math.isclose(t["notional"], 7.5 / 0.225, rel_tol=1e-9)
    assert math.isclose(t["size"], (7.5 / 0.225) / 200.0, rel_tol=1e-9)


def test_position_sizing_capped_at_max_notional() -> None:
    """Tiny std -> raw_notional huge -> capped at 30% of equity = $450."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(6, open_prices=[100] * 6)
    # std=0.1: stop_distance_pct = 1.5 * 0.1 / 100 = 0.0015
    # raw_notional = 7.5 / 0.0015 = 5000 -> capped to 450.
    signals = _empty_signals(candles.index, std_value=0.1)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[4], "exit_target_long"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    t = trades[0]
    assert math.isclose(t["notional"], 450.0, rel_tol=1e-9)
    assert math.isclose(t["size"], 4.5, rel_tol=1e-9)


def test_signal_with_zero_std_is_skipped() -> None:
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(6, open_prices=[100] * 6)
    signals = _empty_signals(candles.index, std_value=0.0)
    signals.loc[candles.index[3], "entry_long"] = True

    trades, skipped = _simulate_symbol(
        "BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0)
    )
    assert trades == []
    assert skipped == 1


# ---------------------------------------------------------------------------
# Fill probability
# ---------------------------------------------------------------------------


def test_zero_fill_probability_skips_all_entries() -> None:
    cfg = StrategyConfig()
    sim_cfg = SimulatorConfig(
        initial_equity=1500.0,
        maker_fee_rate=0.0,
        slippage_bps=0.0,
        fill_prob=0.0,
        seed=0,
    )
    candles = _bars(10, open_prices=[100] * 10)
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[7], "entry_long"] = True

    trades, skipped = _simulate_symbol(
        "BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0)
    )
    assert trades == []
    assert skipped == 2


def test_full_fill_probability_takes_every_entry() -> None:
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(15, open_prices=[100] * 15)
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[4], "exit_target_long"] = True
    signals.loc[candles.index[7], "entry_long"] = True
    signals.loc[candles.index[8], "exit_target_long"] = True

    trades, skipped = _simulate_symbol(
        "BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0)
    )
    assert len(trades) == 2
    assert skipped == 0


# ---------------------------------------------------------------------------
# Exit/entry precedence
# ---------------------------------------------------------------------------


def test_exit_takes_precedence_over_new_entry_on_same_bar() -> None:
    """If we'd exit and re-enter on the same bar, we only exit."""
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(8, open_prices=[100] * 8)
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[5], "exit_target_long"] = True
    # Try to re-enter on the SAME bar 5; this must be ignored.
    signals.loc[candles.index[5], "entry_short"] = True

    trades, _ = _simulate_symbol("BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0))
    assert len(trades) == 1
    assert trades[0]["side"] == "long"
    assert trades[0]["exit_reason"] == "target"


def test_no_concurrent_positions_ignores_entry_while_held() -> None:
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    candles = _bars(10, open_prices=[100] * 10)
    signals = _empty_signals(candles.index, std_value=5.0)
    signals.loc[candles.index[3], "entry_long"] = True
    signals.loc[candles.index[5], "entry_long"] = True  # ignored — already in
    signals.loc[candles.index[7], "exit_target_long"] = True

    trades, skipped = _simulate_symbol(
        "BTC", candles, signals, cfg, sim_cfg, np.random.default_rng(0)
    )
    assert len(trades) == 1
    assert skipped == 0  # the duplicate entry is silently ignored, not "skipped"


# ---------------------------------------------------------------------------
# Integration — 30-bar synthetic with a planted LONG setup
# ---------------------------------------------------------------------------


def _planted_30_bar_long_setup() -> pd.DataFrame:
    """Constant 100 with a single sharp drop at bar 6 then recovery.

    With sma_window=6 and rsi_window=4, this generates exactly one
    LONG entry signal at bar 7 (z[7] = -2.236, rsi[7] = 0) and a
    target exit at bar 8 (z[8] = +0.448 crosses 0).

    `open[t]` is set to `close[t-1]` (a clean no-gap intra-bar model),
    so the entry at bar 7 fills at 80 and the exit at bar 8 fills at 100.
    """
    closes = [100.0] * 6 + [80.0] + [100.0] * 23  # 30 bars
    opens = [closes[0], *closes[:-1]]
    idx = pd.date_range("2026-01-01", periods=30, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes, strict=True)],
            "low": [min(o, c) for o, c in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1.0] * 30,
        },
        index=idx,
    )


def test_integration_30_bar_long_setup_generates_one_trade() -> None:
    cfg = StrategyConfig(
        sma_window=6,
        rsi_window=4,
        z_entry=2.0,
        rsi_oversold=30.0,
        z_target=0.0,
        z_stop=3.5,
        time_stop_bars=2,
    )
    sim_cfg = _zero_cost_sim_cfg()
    candles = _planted_30_bar_long_setup()

    sigs = compute_signals(candles, cfg)
    # The crafted setup should fire exactly one LONG entry.
    assert sigs["entry_long"].sum() == 1
    assert sigs["entry_short"].sum() == 0

    rng = np.random.default_rng(sim_cfg.seed)
    trades, skipped = _simulate_symbol("BTC", candles, sigs, cfg, sim_cfg, rng)
    assert skipped == 0
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["exit_reason"] == "target"
    # Entry at bar 7 (open=80), exit at bar 8 (open=100). Gross pnl positive.
    assert t["entry_price"] == 80.0
    assert t["exit_price"] == 100.0
    assert t["pnl"] > 0


def test_integration_full_run_simulation_aggregates_across_symbols() -> None:
    cfg = StrategyConfig(sma_window=6, rsi_window=4, time_stop_bars=2)
    sim_cfg = _zero_cost_sim_cfg()
    candles_btc = _planted_30_bar_long_setup()
    candles_eth = _planted_30_bar_long_setup()
    result = run_simulation({"BTC": candles_btc, "ETH": candles_eth}, cfg, sim_cfg)

    assert len(result.trades) == 2
    assert set(result.trades["symbol"].unique()) == {"BTC", "ETH"}
    # Equity curve ends above starting equity (both trades are winners here).
    assert result.equity_curve.iloc[-1] > sim_cfg.initial_equity


# ---------------------------------------------------------------------------
# Look-ahead invariance
# ---------------------------------------------------------------------------


def test_look_ahead_mutating_a_past_close_changes_simulator_output() -> None:
    """If the simulator were peeking at future bars, mutations to past
    closes would have no effect on entries that happen later. We verify
    the opposite: changing close[k] makes signals at bar > k differ."""
    cfg = StrategyConfig(sma_window=6, rsi_window=4, time_stop_bars=2)
    sim_cfg = _zero_cost_sim_cfg()

    candles_a = _planted_30_bar_long_setup()
    candles_b = candles_a.copy()
    # Erase the dip: close[6] back to 100. The LONG signal must disappear.
    candles_b.iloc[6] = candles_b.iloc[5]

    sigs_a = compute_signals(candles_a, cfg)
    sigs_b = compute_signals(candles_b, cfg)

    assert sigs_a["entry_long"].sum() == 1
    assert sigs_b["entry_long"].sum() == 0

    rng_a = np.random.default_rng(sim_cfg.seed)
    rng_b = np.random.default_rng(sim_cfg.seed)
    trades_a, _ = _simulate_symbol("BTC", candles_a, sigs_a, cfg, sim_cfg, rng_a)
    trades_b, _ = _simulate_symbol("BTC", candles_b, sigs_b, cfg, sim_cfg, rng_b)
    assert len(trades_a) == 1
    assert len(trades_b) == 0


def test_look_ahead_mutating_a_future_close_does_not_change_pre_mutation_trades() -> None:
    """Changing close[k] must not affect features (and thus trades) at i <= k.

    With features lagged by one bar, feature[t] uses closes through t-1.
    A mutation at bar k affects feature[k+1] onwards. Trades that
    completed at or before bar k must therefore be identical.
    """
    cfg = StrategyConfig(sma_window=6, rsi_window=4, time_stop_bars=2)
    sim_cfg = _zero_cost_sim_cfg()

    candles_a = _planted_30_bar_long_setup()
    candles_b = candles_a.copy()
    # Mutate a far-future bar that the planted trade can't touch.
    candles_b.iloc[20] = candles_b.iloc[20] * 5.0  # drastic future change

    sigs_a = compute_signals(candles_a, cfg)
    sigs_b = compute_signals(candles_b, cfg)
    rng_a = np.random.default_rng(sim_cfg.seed)
    rng_b = np.random.default_rng(sim_cfg.seed)
    trades_a, _ = _simulate_symbol("BTC", candles_a, sigs_a, cfg, sim_cfg, rng_a)
    trades_b, _ = _simulate_symbol("BTC", candles_b, sigs_b, cfg, sim_cfg, rng_b)

    # The originally-planted trade (entry bar 7, exit bar 8) is unaffected.
    assert len(trades_a) >= 1
    first_a = trades_a[0]
    matching = [t for t in trades_b if t["entry_time"] == first_a["entry_time"]]
    assert len(matching) == 1
    first_b = matching[0]
    assert first_a["entry_price"] == first_b["entry_price"]
    assert first_a["exit_price"] == first_b["exit_price"]
    assert math.isclose(first_a["pnl"], first_b["pnl"], rel_tol=1e-12)


# ---------------------------------------------------------------------------
# Empty / pathological inputs
# ---------------------------------------------------------------------------


def test_run_simulation_with_empty_candles_returns_empty_result() -> None:
    result = run_simulation({})
    assert result.trades.empty
    assert result.equity_curve.empty


def test_run_simulation_with_no_signals_returns_no_trades() -> None:
    cfg = StrategyConfig()
    sim_cfg = _zero_cost_sim_cfg()
    # 10 bars of constant price -> std=0 -> no signals.
    candles = _bars(120, open_prices=[100.0] * 120)
    result = run_simulation({"BTC": candles}, cfg, sim_cfg)
    assert result.trades.empty


@pytest.mark.parametrize("seed", [0, 1, 42, 1234])
def test_simulation_is_deterministic_for_a_given_seed(seed: int) -> None:
    cfg = StrategyConfig(sma_window=6, rsi_window=4, time_stop_bars=2)
    sim_cfg_a = SimulatorConfig(fill_prob=0.5, seed=seed)
    sim_cfg_b = SimulatorConfig(fill_prob=0.5, seed=seed)
    candles = _planted_30_bar_long_setup()
    a = run_simulation({"BTC": candles}, cfg, sim_cfg_a)
    b = run_simulation({"BTC": candles}, cfg, sim_cfg_b)
    pd.testing.assert_frame_equal(a.trades, b.trades)
    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
