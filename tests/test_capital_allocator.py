"""Tests for the capital allocator — split, degrade, and edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.execution.capital_allocator import (
    CapitalAllocatorConfig,
    allocate,
    default_config,
)


def _cfg() -> CapitalAllocatorConfig:
    return default_config()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_config_sums_with_buffer_to_one() -> None:
    cfg = default_config()
    total = sum(cfg.strategy_max_pct.values(), Decimal("0")) + cfg.buffer_pct
    assert total == Decimal("1.00")


def test_invalid_config_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        CapitalAllocatorConfig(
            strategy_max_pct={"a": Decimal("0.6"), "b": Decimal("0.5")},
            buffer_pct=Decimal("0.0"),
        )


# ---------------------------------------------------------------------------
# No usage → both at nominal max
# ---------------------------------------------------------------------------


def test_no_usage_both_strategies_at_nominal_max() -> None:
    a = allocate(Decimal("1500"), {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")})
    assert a["mean_reversion"].available_notional == Decimal("375.00")  # 25%
    assert a["funding_capture"].available_notional == Decimal("750.00")  # 50%
    assert not a["mean_reversion"].degraded
    assert not a["funding_capture"].degraded


def test_a_at_full_share_b_still_at_nominal_max() -> None:
    """A using its full 25% leaves B at its nominal 50% (buffer untouched)."""
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("375"), "funding_capture": Decimal("0")},
    )
    assert a["mean_reversion"].available_notional == Decimal("0")
    assert a["funding_capture"].available_notional == Decimal("750")
    assert not a["funding_capture"].degraded


def test_b_at_full_share_a_still_at_nominal_max() -> None:
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("0"), "funding_capture": Decimal("750")},
    )
    assert a["funding_capture"].available_notional == Decimal("0")
    assert a["mean_reversion"].available_notional == Decimal("375")


# ---------------------------------------------------------------------------
# Degrade contract — over-allocation eats into the other's budget
# ---------------------------------------------------------------------------


def test_b_degrades_when_a_overshoots_its_share() -> None:
    """A using 50% (twice its nominal 25%) → B's available shrinks to preserve buffer."""
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("750"), "funding_capture": Decimal("0")},
    )
    # equity 1500, buffer 25% = 375 reserved, A used 750, leaves 375 for B.
    assert a["funding_capture"].available_notional == Decimal("375")
    assert a["funding_capture"].degraded is True


def test_a_degrades_when_b_overshoots_its_share() -> None:
    """B using 80% (more than nominal 50%) → A's available shrinks."""
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("0"), "funding_capture": Decimal("1200")},
    )
    # equity 1500 - buffer 375 - B used 1200 = -75 → clamps at 0.
    assert a["mean_reversion"].available_notional == Decimal("0")
    assert a["mean_reversion"].degraded is True


def test_full_degrade_when_a_consumes_everything_minus_buffer() -> None:
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("1125"), "funding_capture": Decimal("0")},
    )
    # 1500 - 375 buffer - 1125 = 0 for B.
    assert a["funding_capture"].available_notional == Decimal("0")


def test_buffer_is_never_consumed() -> None:
    """Even with both strategies maxing nominal, buffer remains."""
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("375"), "funding_capture": Decimal("750")},
    )
    used = a["mean_reversion"].used_notional + a["funding_capture"].used_notional
    assert used + Decimal("375") == Decimal("1500")  # buffer = 25%


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_equity_returns_zero_for_all() -> None:
    a = allocate(Decimal("0"), {"mean_reversion": Decimal("0"), "funding_capture": Decimal("0")})
    assert a["mean_reversion"].available_notional == Decimal("0")
    assert a["funding_capture"].available_notional == Decimal("0")


def test_negative_equity_returns_zero_for_all() -> None:
    a = allocate(Decimal("-100"), {})
    assert a["mean_reversion"].available_notional == Decimal("0")
    assert a["funding_capture"].available_notional == Decimal("0")


def test_missing_strategy_in_used_dict_treated_as_zero() -> None:
    a = allocate(Decimal("1500"), {})
    assert a["mean_reversion"].available_notional == Decimal("375")
    assert a["funding_capture"].available_notional == Decimal("750")


def test_negative_used_clamps_to_zero() -> None:
    a = allocate(
        Decimal("1500"),
        {"mean_reversion": Decimal("-100"), "funding_capture": Decimal("0")},
    )
    assert a["mean_reversion"].used_notional == Decimal("0")
    assert a["mean_reversion"].available_notional == Decimal("375")


def test_custom_strategy_set_works() -> None:
    cfg = CapitalAllocatorConfig(
        strategy_max_pct={"x": Decimal("0.3"), "y": Decimal("0.4")},
        buffer_pct=Decimal("0.3"),
    )
    a = allocate(Decimal("1000"), {"x": Decimal("0"), "y": Decimal("0")}, cfg)
    assert a["x"].available_notional == Decimal("300")
    assert a["y"].available_notional == Decimal("400")


_ = pytest
