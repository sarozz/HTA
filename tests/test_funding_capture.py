"""Tests for the Strategy B (funding capture) scheduler.

Verifies the per-symbol state machine transitions, capital sizing,
threshold gate, and the early-exit path when the capital allocator
shrinks Strategy B's budget below what it's currently holding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.strategies.funding_capture import (
    AccountSnapshot,
    FundingCaptureSymbolState,
    FundingRateInfo,
    evaluate,
    evaluate_symbol,
)


def _now() -> datetime:
    return datetime(2026, 4, 1, 11, 50, 0, tzinfo=timezone.utc)


def _next_tick(now: datetime, minutes_ahead: int = 10) -> datetime:
    return now + timedelta(minutes=minutes_ahead)


def _account(
    equity: Decimal = Decimal("1500"), available: Decimal | None = None
) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity,
        available_notional_for_strategy=(
            available if available is not None else equity * Decimal("0.5")
        ),
    )


def _rate(
    coin: str = "BTC",
    rate: Decimal = Decimal("0.0015"),
    now: datetime | None = None,
    minutes_to_tick: int = 10,
) -> FundingRateInfo:
    now = now or _now()
    return FundingRateInfo(
        coin=coin,
        predicted_rate=rate,
        next_tick_time=_next_tick(now, minutes_to_tick),
        as_of=now,
    )


# ---------------------------------------------------------------------------
# IDLE phase — entry / threshold gate / sizing
# ---------------------------------------------------------------------------


def test_idle_below_threshold_emits_no_target() -> None:
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(_now(), _rate(rate=Decimal("0.0005")), _account(), state)
    assert decision.perp_target is None
    assert decision.spot_target is None
    assert decision.next_state.phase == "idle"


def test_idle_above_threshold_at_entry_window_emits_paired_targets() -> None:
    now = _now()
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(now, _rate(rate=Decimal("0.002"), now=now), _account(), state)
    assert decision.perp_target is not None
    assert decision.spot_target is not None
    # Short perp, long spot: opposite signs, equal magnitude.
    assert decision.perp_target.notional < 0
    assert decision.spot_target.notional > 0
    assert abs(decision.perp_target.notional) == abs(decision.spot_target.notional)
    assert decision.next_state.phase == "entry_pending"
    assert decision.next_state.target_notional == decision.perp_target.notional


def test_idle_too_early_emits_no_target() -> None:
    """30 minutes before tick — outside the entry window."""
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(_now(), _rate(minutes_to_tick=30), _account(), state)
    assert decision.perp_target is None
    assert decision.next_state.phase == "idle"


def test_idle_past_entry_window_emits_no_target() -> None:
    """Less than 9 minutes to tick — too late for the planned entry."""
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(_now(), _rate(minutes_to_tick=2), _account(), state)
    assert decision.perp_target is None
    assert decision.next_state.phase == "idle"


def test_idle_with_zero_capital_skips() -> None:
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(_now(), _rate(), _account(available=Decimal("0")), state)
    assert decision.perp_target is None
    assert decision.next_state.phase == "idle"


def test_idle_sizing_capped_by_available_notional() -> None:
    state = FundingCaptureSymbolState(coin="BTC")
    # Available is 100, nominal is 50% × 1500 = 750 — should clip to 100.
    decision = evaluate_symbol(
        _now(),
        _rate(),
        _account(equity=Decimal("1500"), available=Decimal("100")),
        state,
    )
    assert abs(decision.perp_target.notional) == Decimal("100")


def test_idle_sizing_uses_nominal_when_available_is_higher() -> None:
    state = FundingCaptureSymbolState(coin="BTC")
    decision = evaluate_symbol(
        _now(),
        _rate(),
        _account(equity=Decimal("1500"), available=Decimal("10000")),
        state,
    )
    # 50% × 1500 = 750; not larger than the 10000 cap.
    assert abs(decision.perp_target.notional) == Decimal("750")


# ---------------------------------------------------------------------------
# ENTRY_PENDING phase — fill window
# ---------------------------------------------------------------------------


def test_entry_pending_with_both_legs_filled_transitions_to_holding() -> None:
    now = _now()
    target_tick = _next_tick(now)
    target = Decimal("-750")
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="entry_pending",
        target_tick_time=target_tick,
        entry_attempt_started=now - timedelta(seconds=30),
        target_notional=target,
        perp_size=target,  # short perp filled
        spot_size=-target,  # long spot filled (opposite sign)
    )
    decision = evaluate_symbol(now, _rate(now=now), _account(), state)
    assert decision.perp_target is None
    assert decision.spot_target is None
    assert decision.next_state.phase == "holding"


def test_entry_pending_partial_fill_after_window_unwinds() -> None:
    now = _now()
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="entry_pending",
        target_tick_time=_next_tick(now),
        entry_attempt_started=now - timedelta(seconds=120),  # past 90s window
        target_notional=Decimal("-750"),
        perp_size=Decimal("-750"),  # only perp filled
        spot_size=Decimal("0"),
    )
    decision = evaluate_symbol(now, _rate(now=now), _account(), state)
    assert decision.perp_target is not None
    assert decision.perp_target.notional == Decimal("0")
    assert decision.spot_target is not None
    assert decision.spot_target.notional == Decimal("0")
    assert decision.next_state.phase == "exit_pending"
    assert "fill window expired" in decision.reason


def test_entry_pending_within_window_keeps_waiting() -> None:
    now = _now()
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="entry_pending",
        target_tick_time=_next_tick(now),
        entry_attempt_started=now - timedelta(seconds=30),
        target_notional=Decimal("-750"),
    )
    decision = evaluate_symbol(now, _rate(now=now), _account(), state)
    # Targets re-emitted (router refreshes); state stays entry_pending.
    assert decision.next_state.phase == "entry_pending"
    assert decision.perp_target is not None
    assert decision.perp_target.notional == Decimal("-750")


# ---------------------------------------------------------------------------
# HOLDING phase — through the funding tick
# ---------------------------------------------------------------------------


def test_holding_before_post_tick_window_holds() -> None:
    now = datetime(2026, 4, 1, 12, 0, 30, tzinfo=timezone.utc)  # 30s after tick
    tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="holding",
        target_tick_time=tick,
        target_notional=Decimal("-750"),
        perp_size=Decimal("-750"),
        spot_size=Decimal("750"),
    )
    decision = evaluate_symbol(now, _rate(now=now), _account(), state)
    assert decision.next_state.phase == "holding"
    assert "holding" in decision.reason


def test_holding_at_post_tick_min_seconds_emits_exit() -> None:
    tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = tick + timedelta(seconds=70)  # >60s after tick
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="holding",
        target_tick_time=tick,
        target_notional=Decimal("-750"),
        perp_size=Decimal("-750"),
        spot_size=Decimal("750"),
    )
    decision = evaluate_symbol(now, _rate(now=now), _account(), state)
    assert decision.next_state.phase == "exit_pending"
    assert decision.perp_target.notional == Decimal("0")
    assert decision.spot_target.notional == Decimal("0")
    assert "scheduled exit" in decision.reason


def test_holding_with_capital_starvation_exits_early() -> None:
    """The other strategy expanded → B's available capital halves.

    Per spec: "Strategy B unwinds correctly if Strategy A's position eats
    into its margin budget."
    """
    tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = tick - timedelta(seconds=30)  # before the post-tick exit
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="holding",
        target_tick_time=tick,
        target_notional=Decimal("-750"),
        perp_size=Decimal("-750"),
        spot_size=Decimal("750"),
    )
    # available is just 100 — far below current notional × ratio threshold
    decision = evaluate_symbol(now, _rate(now=now), _account(available=Decimal("100")), state)
    assert decision.next_state.phase == "exit_pending"
    assert "capital starvation" in decision.reason


# ---------------------------------------------------------------------------
# EXIT_PENDING phase
# ---------------------------------------------------------------------------


def test_exit_pending_completes_to_idle_when_legs_flat() -> None:
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="exit_pending",
        target_tick_time=_now(),
        target_notional=Decimal("-750"),
        perp_size=Decimal("0"),
        spot_size=Decimal("0"),
    )
    decision = evaluate_symbol(_now(), _rate(), _account(), state)
    assert decision.next_state.phase == "idle"
    assert decision.next_state.target_notional == Decimal("0")
    assert decision.perp_target is None
    assert decision.spot_target is None


def test_exit_pending_with_residual_legs_keeps_emitting_zero_targets() -> None:
    state = FundingCaptureSymbolState(
        coin="BTC",
        phase="exit_pending",
        target_notional=Decimal("-750"),
        perp_size=Decimal("-100"),  # not yet flat
        spot_size=Decimal("0"),
    )
    decision = evaluate_symbol(_now(), _rate(), _account(), state)
    assert decision.next_state.phase == "exit_pending"
    assert decision.perp_target.notional == Decimal("0")


# ---------------------------------------------------------------------------
# Top-level evaluate over multiple coins
# ---------------------------------------------------------------------------


def test_evaluate_runs_all_known_coins() -> None:
    now = _now()
    rates = {
        "BTC": _rate("BTC", Decimal("0.0020"), now),
        "ETH": _rate("ETH", Decimal("0.0005"), now),  # below threshold
    }
    states = {
        "BTC": FundingCaptureSymbolState(coin="BTC"),
        "ETH": FundingCaptureSymbolState(coin="ETH"),
        "SOL": FundingCaptureSymbolState(coin="SOL"),  # no rate quote
    }
    decisions = evaluate(now, rates, _account(), states)
    by_coin = {d.coin: d for d in decisions}
    assert by_coin["BTC"].perp_target is not None  # entry fires
    assert by_coin["ETH"].perp_target is None  # threshold gate
    assert by_coin["SOL"].perp_target is None  # no rate
    assert by_coin["SOL"].next_state.phase == "idle"


def test_evaluate_passes_through_non_idle_state_without_rate() -> None:
    """A symbol HOLDING without a rate update still progresses correctly."""
    now = datetime(2026, 4, 1, 12, 1, 30, tzinfo=timezone.utc)
    tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    states = {
        "BTC": FundingCaptureSymbolState(
            coin="BTC",
            phase="holding",
            target_tick_time=tick,
            target_notional=Decimal("-750"),
            perp_size=Decimal("-750"),
            spot_size=Decimal("750"),
        )
    }
    decisions = evaluate(now, {}, _account(), states)
    assert len(decisions) == 1
    assert decisions[0].next_state.phase == "exit_pending"


# ---------------------------------------------------------------------------
# Sizing degrade contract
# ---------------------------------------------------------------------------


def test_idle_entry_size_scales_with_available_capital() -> None:
    """Same equity, decreasing availability → strictly smaller entry sizes."""
    now = _now()
    state = FundingCaptureSymbolState(coin="BTC")
    big = evaluate_symbol(now, _rate(), _account(available=Decimal("750")), state)
    small = evaluate_symbol(now, _rate(), _account(available=Decimal("200")), state)
    tiny = evaluate_symbol(now, _rate(), _account(available=Decimal("50")), state)
    assert abs(big.perp_target.notional) > abs(small.perp_target.notional)
    assert abs(small.perp_target.notional) > abs(tiny.perp_target.notional)


_ = pytest
