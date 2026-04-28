"""Strategy B (funding capture) — vectorisable, deterministic simulator.

Models the spec exactly (SPEC.md, Strategy B):

  Trigger
    Predicted funding > 0.10% per 1h tick.

  Execution
    1. T-10m: place two ALO orders (SHORT perp at bid, LONG spot at ask),
       same notional, up to 50% of free margin per leg.
    2. If both fill within 90s, hold through the funding tick and receive
       the funding payment on the perp short.
    3. If only one fills within 90s, cancel both and unwind the filled
       leg with a market order. Never run unhedged.
    4. T+1..2m: close both legs with ALO orders at the touch.

  Costs (this simulator)
    - 4 maker fills per successful capture: 4 × 0.015% = 6 bps round-trip
    - 0.5 bps slippage per leg => 4 × 0.5 = 2 bps total round-trip
    - Total cost on success: 8 bps of notional
    - On fill failure: 1 maker fee + 1 taker fee + 2 × slippage on the
      filled-then-unwound leg

  Stochastic component
    - 80% of attempts result in both legs filling; 20% of attempts hit
      the unhedged-leg failure path.

The simulator is pure: deterministic given a (funding_history, config,
seed). The data loader is separate and only matters when running on
real testnet/mainnet data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundingSimConfig:
    """All numeric parameters live here. Defaults match SPEC.md."""

    funding_threshold: float = 0.001  # 0.10% — only positive funding above this
    pre_tick_seconds: int = 600  # T-10m
    fill_window_seconds: int = 90  # both legs must fill within this
    post_tick_min_seconds: int = 60
    post_tick_max_seconds: int = 120
    maker_fee_per_side: float = 0.00015  # 0.015% per fill
    taker_fee: float = 0.00035  # 3.5 bps; used on the market-unwind leg
    slippage_per_leg: float = 0.00005  # 0.5 bps per fill
    paired_fill_probability: float = 0.80
    notional_per_leg_pct: float = 0.50  # of equity (proxy for free margin)
    initial_equity: float = 1500.0
    rng_seed: int = 42


@dataclass(frozen=True)
class CaptureAttempt:
    """One row per funding tick observed."""

    tick_time: datetime  # UTC
    coin: str
    funding_rate: float  # fraction (0.0012 = 0.12%)
    notional: float  # leg notional in USDC ($0 when skipped)
    outcome: str  # 'skipped' | 'success' | 'fill_failure'
    fills: int  # 0, 2, or 4
    funding_pnl: float  # received from the funding payment
    fees_paid: float
    slippage_paid: float
    net_pnl: float  # funding_pnl - fees - slippage
    note: str


@dataclass(frozen=True)
class FundingSimResult:
    attempts: list[CaptureAttempt]
    final_equity: float
    config: FundingSimConfig
    equity_curve: list[float] = field(default_factory=list)


def _equity_after(attempts: list[CaptureAttempt], initial_equity: float) -> list[float]:
    eq = initial_equity
    out: list[float] = [eq]
    for a in attempts:
        eq += a.net_pnl
        out.append(eq)
    return out


def _skipped(tick_time: datetime, coin: str, funding_rate: float, note: str) -> CaptureAttempt:
    return CaptureAttempt(
        tick_time=tick_time,
        coin=coin,
        funding_rate=funding_rate,
        notional=0.0,
        outcome="skipped",
        fills=0,
        funding_pnl=0.0,
        fees_paid=0.0,
        slippage_paid=0.0,
        net_pnl=0.0,
        note=note,
    )


def _success(
    tick_time: datetime,
    coin: str,
    funding_rate: float,
    notional: float,
    config: FundingSimConfig,
) -> CaptureAttempt:
    fees = notional * config.maker_fee_per_side * 4
    slippage = notional * config.slippage_per_leg * 4
    funding_pnl = notional * funding_rate
    net = funding_pnl - fees - slippage
    return CaptureAttempt(
        tick_time=tick_time,
        coin=coin,
        funding_rate=funding_rate,
        notional=notional,
        outcome="success",
        fills=4,
        funding_pnl=funding_pnl,
        fees_paid=fees,
        slippage_paid=slippage,
        net_pnl=net,
        note="both legs filled, held through tick",
    )


def _fill_failure(
    tick_time: datetime,
    coin: str,
    funding_rate: float,
    notional: float,
    config: FundingSimConfig,
) -> CaptureAttempt:
    """One leg fills (1 maker fee), the other doesn't, we market-unwind (1 taker fee).

    Slippage applies to both fills. No funding received because we're flat
    by the tick time.
    """
    fees = notional * (config.maker_fee_per_side + config.taker_fee)
    slippage = notional * config.slippage_per_leg * 2
    net = -fees - slippage
    return CaptureAttempt(
        tick_time=tick_time,
        coin=coin,
        funding_rate=funding_rate,
        notional=notional,
        outcome="fill_failure",
        fills=2,
        funding_pnl=0.0,
        fees_paid=fees,
        slippage_paid=slippage,
        net_pnl=net,
        note="one leg unfilled within 90s; filled leg market-unwound",
    )


def simulate(
    funding_history: pd.DataFrame, config: FundingSimConfig | None = None
) -> FundingSimResult:
    """Simulate Strategy B over the given funding history.

    funding_history columns:
      tick_time : pandas.Timestamp (UTC)
      coin      : str
      funding_rate : float (fraction; 0.0012 = 0.12%)

    Returns one CaptureAttempt per row in funding_history (skipped or
    otherwise) plus aggregates.
    """
    cfg = config or FundingSimConfig()
    if not {"tick_time", "coin", "funding_rate"} <= set(funding_history.columns):
        raise ValueError("funding_history must have columns: tick_time, coin, funding_rate")
    rng = np.random.default_rng(cfg.rng_seed)
    attempts: list[CaptureAttempt] = []
    equity = cfg.initial_equity

    # Sort chronologically so equity compounds in time order. Ties broken
    # by coin so behaviour is deterministic.
    df = funding_history.sort_values(["tick_time", "coin"]).reset_index(drop=True)

    for row in df.itertuples(index=False):
        tick_time = pd.Timestamp(row.tick_time)
        if tick_time.tzinfo is None:
            tick_time = tick_time.tz_localize(timezone.utc)
        py_dt = tick_time.to_pydatetime()
        coin = str(row.coin)
        rate = float(row.funding_rate)

        attempt: CaptureAttempt
        if equity <= 0:
            attempt = _skipped(py_dt, coin, rate, "no equity")
        elif rate <= cfg.funding_threshold:
            attempt = _skipped(
                py_dt, coin, rate, f"at-or-below threshold {cfg.funding_threshold:.4%}"
            )
        else:
            notional = equity * cfg.notional_per_leg_pct
            roll = rng.random()
            if roll < cfg.paired_fill_probability:
                attempt = _success(py_dt, coin, rate, notional, cfg)
            else:
                attempt = _fill_failure(py_dt, coin, rate, notional, cfg)
        attempts.append(attempt)
        equity += attempt.net_pnl

    return FundingSimResult(
        attempts=attempts,
        final_equity=equity,
        config=cfg,
        equity_curve=_equity_after(attempts, cfg.initial_equity),
    )


# --------------------------------------------------------------------------
# Convenience aggregations used by the report generator
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingSimStats:
    total_ticks: int
    skipped: int
    attempted: int
    succeeded: int
    failed: int
    win_rate: float  # successes / (successes + failures); 0 if no attempts
    funding_received: float
    fees_paid: float
    slippage_paid: float
    total_pnl: float
    final_equity: float
    initial_equity: float


def aggregate(result: FundingSimResult) -> FundingSimStats:
    attempts = result.attempts
    total = len(attempts)
    skipped = sum(1 for a in attempts if a.outcome == "skipped")
    succeeded = sum(1 for a in attempts if a.outcome == "success")
    failed = sum(1 for a in attempts if a.outcome == "fill_failure")
    attempted = succeeded + failed
    win_rate = succeeded / attempted if attempted > 0 else 0.0
    return FundingSimStats(
        total_ticks=total,
        skipped=skipped,
        attempted=attempted,
        succeeded=succeeded,
        failed=failed,
        win_rate=win_rate,
        funding_received=sum(a.funding_pnl for a in attempts),
        fees_paid=sum(a.fees_paid for a in attempts),
        slippage_paid=sum(a.slippage_paid for a in attempts),
        total_pnl=sum(a.net_pnl for a in attempts),
        final_equity=result.final_equity,
        initial_equity=result.config.initial_equity,
    )


def attempts_to_dataframe(attempts: list[CaptureAttempt]) -> pd.DataFrame:
    """Convert attempts to a tidy DataFrame for reporting / analysis."""
    return pd.DataFrame(
        [
            {
                "tick_time": a.tick_time,
                "coin": a.coin,
                "funding_rate": a.funding_rate,
                "notional": a.notional,
                "outcome": a.outcome,
                "fills": a.fills,
                "funding_pnl": a.funding_pnl,
                "fees": a.fees_paid,
                "slippage": a.slippage_paid,
                "net_pnl": a.net_pnl,
                "note": a.note,
            }
            for a in attempts
        ]
    )
