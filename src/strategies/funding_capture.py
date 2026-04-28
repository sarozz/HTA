"""Strategy B — funding-capture scheduler. Pure functions only.

`evaluate(now, funding_rates, available_notional, state)` returns paired
TargetPositions (perp + spot, opposite signs) plus the next state. The
caller is responsible for state persistence and submitting the targets
through the order router.

Per-symbol state machine, advanced by `evaluate`:

    IDLE ──── (T-10m, predicted > threshold) ───────► ENTRY_PENDING
    ENTRY_PENDING ── (both legs filled) ───────────► HOLDING
    ENTRY_PENDING ── (90s elapsed, partial fill) ──► EXIT_PENDING (unwind)
    HOLDING ──── (T+1m..T+2m, scheduled) ──────────► EXIT_PENDING
    EXIT_PENDING ── (both legs flat) ──────────────► IDLE

Sizing degrade contract:
  At ENTRY_PENDING transition, `available_notional` is the size cap
  from the capital allocator. We use min(notional_per_leg_pct × equity,
  available_notional). If `available_notional <= 0`, we skip the
  capture entirely and stay IDLE.

If a HOLDING capture finds itself with insufficient capital (e.g. the
other strategy has expanded), this scheduler emits early exit targets
to honour the "scale down its size accordingly" requirement.

The spot leg is emitted as a TargetPosition so a future spot router can
consume it. Today's order router only handles perps; main.py logs and
gates the spot leg until that route is wired.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from src.strategies.mean_reversion import TargetPosition

logger = logging.getLogger(__name__)

Phase = Literal["idle", "entry_pending", "holding", "exit_pending"]
PERP_VENUE = "perp"
SPOT_VENUE = "spot"


@dataclass(frozen=True)
class FundingCaptureConfig:
    funding_threshold: Decimal = Decimal("0.001")  # 0.10%, strict greater-than
    pre_tick_seconds: int = 600  # T-10m
    fill_window_seconds: int = 90  # both legs must fill within this
    post_tick_min_seconds: int = 60
    post_tick_max_seconds: int = 120
    notional_per_leg_pct: Decimal = Decimal("0.50")  # of equity proxy, capped by allocator
    early_exit_capital_ratio: Decimal = Decimal("0.5")  # exit if avail < this × current notional


@dataclass(frozen=True)
class FundingRateInfo:
    """Snapshot of funding info for one symbol at `as_of`."""

    coin: str
    predicted_rate: Decimal  # fraction; 0.0012 = 0.12%
    next_tick_time: datetime  # UTC; the upcoming 1h funding tick
    as_of: datetime


@dataclass(frozen=True)
class FundingCaptureSymbolState:
    """Per-symbol persistent state that the caller threads through evaluate."""

    coin: str
    phase: Phase = "idle"
    target_tick_time: datetime | None = None  # the tick we're (or were) capturing
    entry_attempt_started: datetime | None = None
    target_notional: Decimal = Decimal("0")  # signed: negative = short perp / long spot pair
    perp_size: Decimal = Decimal("0")  # signed; current perp position
    spot_size: Decimal = Decimal("0")  # signed; current spot position


@dataclass(frozen=True)
class FundingCaptureDecision:
    coin: str
    perp_target: TargetPosition | None
    spot_target: TargetPosition | None
    next_state: FundingCaptureSymbolState
    reason: str


@dataclass(frozen=True)
class AccountSnapshot:
    """Subset of account state the scheduler needs."""

    equity: Decimal
    available_notional_for_strategy: Decimal


def _both_legs_filled(state: FundingCaptureSymbolState) -> bool:
    """Both legs are filled when their absolute sizes match the target.

    The pair is short-perp + long-spot, equal magnitude, opposite signs.
    """
    target_mag = abs(state.target_notional)
    if target_mag == 0:
        return False
    perp_filled = state.perp_size * state.target_notional > 0 and abs(
        state.perp_size
    ) >= target_mag * Decimal("0.99")
    spot_filled = state.spot_size * state.target_notional < 0 and abs(
        state.spot_size
    ) >= target_mag * Decimal("0.99")
    # target_notional convention: negative magnitude (we short perp, long spot
    # for positive funding). perp sign matches target sign; spot is opposite.
    return perp_filled and spot_filled


def _both_legs_flat(state: FundingCaptureSymbolState) -> bool:
    return state.perp_size == 0 and state.spot_size == 0


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _enter_pair(
    state: FundingCaptureSymbolState,
    now: datetime,
    target_tick_time: datetime,
    notional: Decimal,
    config: FundingCaptureConfig,
) -> FundingCaptureDecision:
    """Emit paired entry targets. Pair direction: SHORT perp + LONG spot.

    target_notional encodes the perp side (negative for short).
    """
    perp_target_notional = -notional  # short perp
    spot_target_notional = notional  # long spot
    perp = TargetPosition(
        symbol=state.coin,
        notional=perp_target_notional,
        reason=f"funding entry T-{config.pre_tick_seconds}s",
    )
    spot = TargetPosition(
        symbol=f"{state.coin}-SPOT",
        notional=spot_target_notional,
        reason="funding entry hedge",
    )
    next_state = replace(
        state,
        phase="entry_pending",
        target_tick_time=target_tick_time,
        entry_attempt_started=now,
        target_notional=perp_target_notional,
    )
    return FundingCaptureDecision(
        coin=state.coin,
        perp_target=perp,
        spot_target=spot,
        next_state=next_state,
        reason=f"enter pair notional=${notional}",
    )


def _exit_pair(state: FundingCaptureSymbolState, reason: str) -> FundingCaptureDecision:
    """Emit paired exit targets — both legs go flat."""
    perp = TargetPosition(symbol=state.coin, notional=Decimal("0"), reason=reason)
    spot = TargetPosition(symbol=f"{state.coin}-SPOT", notional=Decimal("0"), reason=reason)
    next_state = replace(state, phase="exit_pending")
    return FundingCaptureDecision(
        coin=state.coin,
        perp_target=perp,
        spot_target=spot,
        next_state=next_state,
        reason=reason,
    )


def _no_op(state: FundingCaptureSymbolState, reason: str) -> FundingCaptureDecision:
    return FundingCaptureDecision(
        coin=state.coin,
        perp_target=None,
        spot_target=None,
        next_state=state,
        reason=reason,
    )


def _holding_perp_target(state: FundingCaptureSymbolState, reason: str) -> FundingCaptureDecision:
    """During HOLDING phase the targets stay equal to current position."""
    perp = TargetPosition(
        symbol=state.coin,
        notional=state.target_notional,
        reason=reason,
    )
    spot = TargetPosition(
        symbol=f"{state.coin}-SPOT",
        notional=-state.target_notional,
        reason=reason,
    )
    return FundingCaptureDecision(
        coin=state.coin,
        perp_target=perp,
        spot_target=spot,
        next_state=state,
        reason=reason,
    )


def evaluate_symbol(
    now: datetime,
    rate_info: FundingRateInfo,
    account: AccountSnapshot,
    state: FundingCaptureSymbolState,
    config: FundingCaptureConfig | None = None,
) -> FundingCaptureDecision:
    """Advance the per-symbol state machine and emit targets if needed."""
    cfg = config or FundingCaptureConfig()
    now_utc = _ensure_utc(now)
    next_tick = _ensure_utc(rate_info.next_tick_time)
    seconds_to_tick = (next_tick - now_utc).total_seconds()

    # ---- HOLDING / EXIT_PENDING — drive the trade through the funding tick.

    if state.phase == "holding":
        seconds_after_tick = (
            (now_utc - _ensure_utc(state.target_tick_time)).total_seconds()
            if state.target_tick_time
            else 0
        )
        # Capital starvation — the other strategy expanded into our budget.
        # If our headroom drops below half (default) of what we're holding,
        # bail before the post-tick window so we don't get force-closed.
        current_notional = abs(state.target_notional)
        if (
            current_notional > 0
            and account.available_notional_for_strategy
            < current_notional * cfg.early_exit_capital_ratio
        ):
            return _exit_pair(state, "early exit: capital starvation")
        # Scheduled exit: 60-120s after tick.
        if seconds_after_tick >= cfg.post_tick_min_seconds:
            return _exit_pair(
                state,
                f"scheduled exit T+{int(seconds_after_tick)}s",
            )
        return _holding_perp_target(state, f"holding T+{int(seconds_after_tick)}s")

    if state.phase == "exit_pending":
        if _both_legs_flat(state):
            next_state = replace(
                state,
                phase="idle",
                target_tick_time=None,
                entry_attempt_started=None,
                target_notional=Decimal("0"),
            )
            return FundingCaptureDecision(
                coin=state.coin,
                perp_target=None,
                spot_target=None,
                next_state=next_state,
                reason="exit complete; back to idle",
            )
        return _exit_pair(state, "awaiting flat")

    # ---- ENTRY_PENDING — wait for both legs to fill within 90s.

    if state.phase == "entry_pending":
        elapsed = (
            (now_utc - _ensure_utc(state.entry_attempt_started)).total_seconds()
            if state.entry_attempt_started
            else 0.0
        )
        if _both_legs_filled(state):
            next_state = replace(state, phase="holding")
            return FundingCaptureDecision(
                coin=state.coin,
                perp_target=None,
                spot_target=None,
                next_state=next_state,
                reason="both legs filled; holding through tick",
            )
        if elapsed > cfg.fill_window_seconds:
            return _exit_pair(state, "fill window expired; unwind")
        # Continue waiting; re-emit current targets so the router refreshes.
        perp = TargetPosition(
            symbol=state.coin, notional=state.target_notional, reason="awaiting fills"
        )
        spot = TargetPosition(
            symbol=f"{state.coin}-SPOT",
            notional=-state.target_notional,
            reason="awaiting fills",
        )
        return FundingCaptureDecision(
            coin=state.coin,
            perp_target=perp,
            spot_target=spot,
            next_state=state,
            reason=f"entry pending {int(elapsed)}s",
        )

    # ---- IDLE — only entry path.
    if rate_info.predicted_rate <= cfg.funding_threshold:
        return _no_op(state, f"predicted {rate_info.predicted_rate} <= threshold")

    if seconds_to_tick > cfg.pre_tick_seconds + 60:
        return _no_op(state, f"too early; {int(seconds_to_tick)}s to tick")
    if seconds_to_tick < cfg.pre_tick_seconds - 60:
        # Missed the entry window for this tick; wait for the next one.
        return _no_op(state, f"past entry window; {int(seconds_to_tick)}s to tick")

    if account.available_notional_for_strategy <= 0:
        return _no_op(state, "no capital available")

    # Sizing: nominal is notional_per_leg_pct of equity, capped by allocator.
    nominal = account.equity * cfg.notional_per_leg_pct
    notional = min(nominal, account.available_notional_for_strategy)
    if notional <= 0:
        return _no_op(state, "computed notional <= 0")

    return _enter_pair(state, now_utc, next_tick, notional, cfg)


def evaluate(
    now: datetime,
    funding_rates: dict[str, FundingRateInfo],
    account: AccountSnapshot,
    states: dict[str, FundingCaptureSymbolState],
    config: FundingCaptureConfig | None = None,
) -> list[FundingCaptureDecision]:
    """Evaluate every symbol that has either a state or a rate quote.

    Symbols without a current state are treated as IDLE for the symbol.
    """
    cfg = config or FundingCaptureConfig()
    coins = sorted(set(funding_rates) | set(states))
    out: list[FundingCaptureDecision] = []
    for coin in coins:
        rate = funding_rates.get(coin)
        state = states.get(coin) or FundingCaptureSymbolState(coin=coin)
        if rate is None:
            # Without a rate, only HOLDING/EXIT_PENDING/ENTRY_PENDING can
            # progress. Construct a placeholder rate that won't trigger entry.
            placeholder = FundingRateInfo(
                coin=coin,
                predicted_rate=Decimal("0"),
                next_tick_time=now + timedelta(hours=1),
                as_of=now,
            )
            out.append(evaluate_symbol(now, placeholder, account, state, cfg))
        else:
            out.append(evaluate_symbol(now, rate, account, state, cfg))
    return out
