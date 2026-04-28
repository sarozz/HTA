"""Capital allocator — splits equity between strategies, preserving a buffer.

Defaults:
  Strategy A (mean reversion) : up to 25% of equity in margin
  Strategy B (funding capture): up to 50% of equity in margin
  Reserved buffer             : 25% of equity (never used)

Degrade-not-reject contract:
  If Strategy A is consuming more than its nominal share (e.g. due to
  mark-price moves expanding notional), Strategy B's available budget
  shrinks proportionally to preserve the buffer. B is given the lesser
  of:
      its nominal max share              (0.50 × equity)
      and the residual after A + buffer  (max(0, equity - A_used - buffer))

  The same logic applies symmetrically to A given B's usage.

The allocator is a pure function — the caller is responsible for
querying the position tracker, computing per-strategy notional, and
acting on the returned allocations.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StrategyAllocation:
    strategy: str
    max_pct: Decimal  # nominal share of equity this strategy may use
    used_notional: Decimal  # current notional this strategy is using
    available_notional: Decimal  # how much MORE notional can be opened
    degraded: bool  # True iff available < (equity * max_pct - used)


@dataclass(frozen=True)
class CapitalAllocatorConfig:
    """Per-strategy max share of equity. Sum + buffer should be <= 1.0."""

    strategy_max_pct: dict[str, Decimal]
    buffer_pct: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        total = sum(self.strategy_max_pct.values(), Decimal("0")) + self.buffer_pct
        if total > Decimal("1.0"):
            raise ValueError(
                f"strategy allocations + buffer = {total} exceeds 1.0; " f"please rebalance"
            )


def default_config() -> CapitalAllocatorConfig:
    return CapitalAllocatorConfig(
        strategy_max_pct={
            "mean_reversion": Decimal("0.25"),
            "funding_capture": Decimal("0.50"),
        },
        buffer_pct=Decimal("0.25"),
    )


def allocate(
    equity: Decimal,
    used_by_strategy: dict[str, Decimal],
    config: CapitalAllocatorConfig | None = None,
) -> dict[str, StrategyAllocation]:
    """Compute available notional per strategy.

    `used_by_strategy` is the current notional each strategy is using
    (positive numbers). Strategies absent from the map get 0 used.

    Returns a mapping from strategy name to its `StrategyAllocation`.
    Strategies not declared in config are not returned.
    """
    cfg = config or default_config()
    if equity <= 0:
        return {
            name: StrategyAllocation(
                strategy=name,
                max_pct=cfg.strategy_max_pct[name],
                used_notional=Decimal("0"),
                available_notional=Decimal("0"),
                degraded=False,
            )
            for name in cfg.strategy_max_pct
        }

    buffer = equity * cfg.buffer_pct
    out: dict[str, StrategyAllocation] = {}

    for name, max_pct in cfg.strategy_max_pct.items():
        used = max(Decimal("0"), used_by_strategy.get(name, Decimal("0")))
        nominal_max = equity * max_pct
        nominal_available = max(Decimal("0"), nominal_max - used)

        # Other strategies' usage eats into the residual after the buffer.
        other_used = sum(
            (
                max(Decimal("0"), used_by_strategy.get(other, Decimal("0")))
                for other in cfg.strategy_max_pct
                if other != name
            ),
            Decimal("0"),
        )
        residual = max(Decimal("0"), equity - buffer - other_used - used)

        available = min(nominal_available, residual)
        degraded = available < nominal_available
        out[name] = StrategyAllocation(
            strategy=name,
            max_pct=max_pct,
            used_notional=used,
            available_notional=available,
            degraded=degraded,
        )
    return out
