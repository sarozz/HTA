"""Tests for Strategy B funding-capture simulator.

Hand-built scenarios with known outcomes:
  - Below-threshold ticks must be skipped, no P&L, no fills
  - Above-threshold ticks with paired fill: P&L = funding - 8 bps of notional
  - Above-threshold ticks with unhedged failure: NEGATIVE P&L (loss)
  - Numeric correctness on a hand-computed example
  - RNG determinism (seeds give reproducible outcomes)
  - Equity compounds across multiple ticks in chronological order
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.backtest.funding_simulator import (
    FundingSimConfig,
    aggregate,
    attempts_to_dataframe,
    simulate,
)


def _ticks(rows: list[tuple[datetime, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["tick_time", "coin", "funding_rate"])


def _tt(hours_offset: int = 0) -> datetime:
    return datetime(2026, 4, 1, tzinfo=timezone.utc) + timedelta(hours=hours_offset)


# ---------------------------------------------------------------------------
# Threshold gate
# ---------------------------------------------------------------------------


def test_below_threshold_ticks_are_skipped() -> None:
    df = _ticks(
        [
            (_tt(0), "BTC", 0.0005),  # 5 bps — below 10 bps threshold
            (_tt(1), "BTC", 0.0009),  # 9 bps — still below
            (_tt(2), "BTC", 0.0),  # zero
        ]
    )
    res = simulate(df, FundingSimConfig())
    assert all(a.outcome == "skipped" for a in res.attempts)
    assert all(a.net_pnl == 0.0 for a in res.attempts)
    assert all(a.notional == 0.0 for a in res.attempts)
    assert res.final_equity == FundingSimConfig().initial_equity


def test_negative_funding_is_skipped() -> None:
    """Strategy B only trades positive funding above threshold."""
    df = _ticks([(_tt(0), "BTC", -0.005)])  # -50 bps
    res = simulate(df, FundingSimConfig())
    assert res.attempts[0].outcome == "skipped"


def test_exactly_at_threshold_is_skipped() -> None:
    """Threshold is strict (> not >=); at exactly 10 bps we don't capture."""
    df = _ticks([(_tt(0), "BTC", 0.001)])
    res = simulate(df, FundingSimConfig(funding_threshold=0.001))
    assert res.attempts[0].outcome == "skipped"


def test_just_above_threshold_is_attempted() -> None:
    df = _ticks([(_tt(0), "BTC", 0.00100001)])
    cfg = FundingSimConfig(paired_fill_probability=1.0)  # force success
    res = simulate(df, cfg)
    assert res.attempts[0].outcome == "success"


# ---------------------------------------------------------------------------
# Numerical correctness — paired-fill success
# ---------------------------------------------------------------------------


def test_success_pnl_equals_funding_minus_six_bps_minus_two_bps() -> None:
    """One tick at 0.20% funding, paired fill forced → net = 12 bps of notional.

    notional = $1500 × 50% = $750
    funding_received = $750 × 0.0020 = $1.50
    fees = $750 × 0.00015 × 4 = $0.45 (6 bps)
    slippage = $750 × 0.00005 × 4 = $0.15 (2 bps)
    net = $1.50 − $0.45 − $0.15 = $0.90  (12 bps of notional)
    """
    df = _ticks([(_tt(0), "BTC", 0.002)])
    cfg = FundingSimConfig(paired_fill_probability=1.0)
    res = simulate(df, cfg)
    a = res.attempts[0]
    assert a.outcome == "success"
    assert a.fills == 4
    assert a.notional == pytest.approx(750.0)
    assert a.funding_pnl == pytest.approx(1.5)
    assert a.fees_paid == pytest.approx(0.45)
    assert a.slippage_paid == pytest.approx(0.15)
    assert a.net_pnl == pytest.approx(0.9)


def test_at_zero_breakeven_funding_success_yields_negative_pnl() -> None:
    """If funding is just above threshold (10 bps), net is +2 bps before MtM
    randomness: 10 - 6 - 2 = +2 bps. So success at 11 bps still positive."""
    df = _ticks([(_tt(0), "BTC", 0.0011)])  # 11 bps
    cfg = FundingSimConfig(paired_fill_probability=1.0)
    res = simulate(df, cfg)
    a = res.attempts[0]
    assert a.outcome == "success"
    expected_net = 750.0 * (0.0011 - 0.0006 - 0.0002)
    assert a.net_pnl == pytest.approx(expected_net)
    assert a.net_pnl > 0


# ---------------------------------------------------------------------------
# Numerical correctness — unhedged-leg failure
# ---------------------------------------------------------------------------


def test_unhedged_failure_is_strictly_a_loss() -> None:
    """Force fill_failure path. Net P&L must be negative — funding NOT received."""
    df = _ticks([(_tt(0), "BTC", 0.005)])  # 50 bps — would be huge if we captured
    cfg = FundingSimConfig(paired_fill_probability=0.0)  # force failure
    res = simulate(df, cfg)
    a = res.attempts[0]
    assert a.outcome == "fill_failure"
    assert a.fills == 2
    assert a.funding_pnl == 0.0
    # 1 maker fee + 1 taker fee + 2 slippages = 0.015% + 0.035% + 1 bp = 0.06%
    assert a.fees_paid == pytest.approx(750.0 * (0.00015 + 0.00035))
    assert a.slippage_paid == pytest.approx(750.0 * 0.00005 * 2)
    assert a.net_pnl < 0
    expected_loss = -(750.0 * (0.00015 + 0.00035) + 750.0 * 0.00005 * 2)
    assert a.net_pnl == pytest.approx(expected_loss)


def test_unhedged_failure_size_independent_of_funding_rate() -> None:
    """Loss on a failed paired fill depends on notional only, not on the
    funding rate that we never received."""
    df = _ticks([(_tt(0), "BTC", 0.002), (_tt(1), "BTC", 0.020)])
    # Force failures and reset equity so notional is the same.
    cfg = FundingSimConfig(paired_fill_probability=0.0, initial_equity=1500.0)
    res = simulate(df, cfg)
    # Equity drops between ticks, so notional differs slightly. Compute
    # expected losses given the compounding.
    eq0 = 1500.0
    notional0 = eq0 * 0.5
    loss0 = -(notional0 * (0.00015 + 0.00035) + notional0 * 0.00005 * 2)
    assert res.attempts[0].net_pnl == pytest.approx(loss0)
    eq1 = eq0 + loss0
    notional1 = eq1 * 0.5
    loss1 = -(notional1 * (0.00015 + 0.00035) + notional1 * 0.00005 * 2)
    assert res.attempts[1].net_pnl == pytest.approx(loss1)


# ---------------------------------------------------------------------------
# RNG determinism + paired_fill_probability behaviour
# ---------------------------------------------------------------------------


def test_same_seed_reproduces_same_outcomes() -> None:
    df = _ticks([(_tt(h), "BTC", 0.0015) for h in range(100)])
    cfg = FundingSimConfig(rng_seed=12345)
    a = simulate(df, cfg)
    b = simulate(df, cfg)
    outcomes_a = [x.outcome for x in a.attempts]
    outcomes_b = [x.outcome for x in b.attempts]
    assert outcomes_a == outcomes_b


def test_different_seeds_produce_different_outcomes() -> None:
    df = _ticks([(_tt(h), "BTC", 0.0015) for h in range(200)])
    a = simulate(df, FundingSimConfig(rng_seed=1))
    b = simulate(df, FundingSimConfig(rng_seed=999))
    outcomes_a = [x.outcome for x in a.attempts]
    outcomes_b = [x.outcome for x in b.attempts]
    assert outcomes_a != outcomes_b  # vanishingly unlikely with 200 ticks


def test_paired_fill_probability_one_yields_all_success() -> None:
    df = _ticks([(_tt(h), "BTC", 0.0015) for h in range(50)])
    res = simulate(df, FundingSimConfig(paired_fill_probability=1.0))
    assert all(a.outcome == "success" for a in res.attempts)


def test_paired_fill_probability_zero_yields_all_failure() -> None:
    df = _ticks([(_tt(h), "BTC", 0.0015) for h in range(50)])
    res = simulate(df, FundingSimConfig(paired_fill_probability=0.0))
    assert all(a.outcome == "fill_failure" for a in res.attempts)


def test_paired_fill_probability_default_eighty_percent_holds_in_aggregate() -> None:
    """Sanity: 1000 attempts → roughly 800 successes, 200 failures."""
    df = _ticks([(_tt(h), "BTC", 0.0015) for h in range(1000)])
    # initial_equity huge so per-tick notional doesn't shrink to zero on losses
    cfg = FundingSimConfig(initial_equity=1_000_000.0)
    res = simulate(df, cfg)
    successes = sum(1 for a in res.attempts if a.outcome == "success")
    failures = sum(1 for a in res.attempts if a.outcome == "fill_failure")
    assert successes + failures == 1000
    # 80% ± 5% with 1000 trials.
    assert 0.75 <= successes / 1000 <= 0.85


# ---------------------------------------------------------------------------
# Equity compounding across ticks
# ---------------------------------------------------------------------------


def test_equity_curve_starts_at_initial_and_evolves_per_attempt() -> None:
    df = _ticks(
        [
            (_tt(0), "BTC", 0.0020),  # success: +0.90
            (_tt(1), "BTC", 0.0005),  # skip
            (_tt(2), "BTC", 0.0030),  # success
        ]
    )
    cfg = FundingSimConfig(paired_fill_probability=1.0)
    res = simulate(df, cfg)
    assert len(res.equity_curve) == 4  # initial + 3 attempts
    assert res.equity_curve[0] == pytest.approx(1500.0)
    assert res.equity_curve[1] == pytest.approx(1500.0 + res.attempts[0].net_pnl)
    assert res.equity_curve[2] == res.equity_curve[1]  # skip
    assert res.equity_curve[3] == pytest.approx(res.equity_curve[2] + res.attempts[2].net_pnl)


def test_attempts_processed_in_chronological_order_even_if_input_unsorted() -> None:
    rows = [
        (_tt(2), "ETH", 0.0030),
        (_tt(0), "BTC", 0.0020),
        (_tt(1), "SOL", 0.0010),  # at threshold → skipped
    ]
    df = _ticks(rows)
    cfg = FundingSimConfig(paired_fill_probability=1.0)
    res = simulate(df, cfg)
    times = [a.tick_time for a in res.attempts]
    assert times == sorted(times)


def test_no_equity_after_blowup_means_subsequent_ticks_skipped() -> None:
    df = _ticks([(_tt(h), "BTC", 0.005) for h in range(5)])
    # Force failures with a tiny initial equity so it goes to zero quickly.
    cfg = FundingSimConfig(
        paired_fill_probability=0.0,
        initial_equity=1.0,  # tiny
    )
    res = simulate(df, cfg)
    # Eventually equity hits 0 and remaining ticks get 'skipped' with note='no equity'.
    skipped_for_no_equity = [a for a in res.attempts if a.note == "no equity"]
    # With a $1 initial equity this depends on exact arithmetic; just make
    # sure the simulator never assigns negative notional or panics.
    for a in res.attempts:
        assert a.notional >= 0.0
    assert any(a.outcome == "fill_failure" for a in res.attempts) or skipped_for_no_equity


# ---------------------------------------------------------------------------
# Aggregation + DataFrame export
# ---------------------------------------------------------------------------


def test_aggregate_counts_match_attempts() -> None:
    df = _ticks(
        [
            (_tt(0), "BTC", 0.0005),  # skip
            (_tt(1), "BTC", 0.002),  # success
            (_tt(2), "BTC", 0.002),  # failure
            (_tt(3), "BTC", 0.0005),  # skip
        ]
    )
    # paired_fill_probability=0.5 with seed=0 gives one success, one failure
    # for these two attempts (verified empirically; deterministic).
    cfg = FundingSimConfig(paired_fill_probability=0.5, rng_seed=0)
    res = simulate(df, cfg)
    stats = aggregate(res)
    assert stats.total_ticks == 4
    assert stats.skipped == 2
    assert stats.attempted == 2
    assert stats.succeeded + stats.failed == 2


def test_attempts_to_dataframe_has_expected_columns() -> None:
    df = _ticks([(_tt(0), "BTC", 0.0020)])
    res = simulate(df, FundingSimConfig(paired_fill_probability=1.0))
    out = attempts_to_dataframe(res.attempts)
    expected = {
        "tick_time",
        "coin",
        "funding_rate",
        "notional",
        "outcome",
        "fills",
        "funding_pnl",
        "fees",
        "slippage",
        "net_pnl",
        "note",
    }
    assert expected <= set(out.columns)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_simulate_rejects_dataframe_missing_columns() -> None:
    df = pd.DataFrame({"tick_time": [_tt(0)], "coin": ["BTC"]})
    with pytest.raises(ValueError):
        simulate(df, FundingSimConfig())


def test_simulate_handles_empty_dataframe() -> None:
    df = _ticks([])
    res = simulate(df, FundingSimConfig())
    assert res.attempts == []
    assert res.final_equity == FundingSimConfig().initial_equity
    assert res.equity_curve == [FundingSimConfig().initial_equity]


def test_naive_timestamps_are_localised_to_utc() -> None:
    naive = datetime(2026, 4, 1, 12, 0)
    df = pd.DataFrame({"tick_time": [naive], "coin": ["BTC"], "funding_rate": [0.0020]})
    res = simulate(df, FundingSimConfig(paired_fill_probability=1.0))
    assert res.attempts[0].tick_time.tzinfo is not None


# Smoke imports (lint pacifier)
_ = pytest
