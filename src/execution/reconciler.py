"""Reconciler — every 30s, compare local cache to exchange truth.

Per RISK.md reconciliation contract:
  Every 30 seconds, the Reconciler MUST
    1. Query the exchange for actual positions and open orders.
    2. Compare to local state.
    3. On any divergence, call halt("reconciliation divergence: ...").

Local state lives in PositionTracker. The reconciler refreshes the
tracker as part of the check — that gets us the exchange's view — and
compares that to a snapshot of the cache taken just before the refresh.
Any difference triggers a halt.

A loop tick that itself fails (e.g. exchange unreachable) also halts —
inability to verify state is itself a divergence we can't ignore.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from src.execution.position_tracker import PositionTracker, TrackedOrder, TrackedPosition
from src.risk.kill_switch import KillSwitch
from src.storage.journal import JournalLike

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        tracker: PositionTracker,
        kill_switch: KillSwitch,
        journal: JournalLike,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self._tracker = tracker
        self._kill_switch = kill_switch
        self._journal = journal
        self._interval = interval_seconds
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Main loop. Returns cleanly when stop() is called or task cancelled."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                return  # stop signalled
            except asyncio.TimeoutError:
                pass
            await self.check_once()

    async def check_once(self) -> None:
        """Run one reconciliation pass. Halts on any divergence or error."""
        if self._kill_switch.halted:
            return  # already halted; don't re-trigger

        # Snapshot the cache BEFORE refresh so we can diff old (pre-refresh
        # belief) vs new (truth from exchange). The strategy + router only
        # consume the cache, so any drift between them and the exchange
        # showed up as old != new.
        old_positions = {p.symbol: p for p in self._tracker.positions()}
        old_orders = {o.oid: o for o in self._tracker.open_orders()}

        try:
            await self._tracker.refresh()
        except Exception as exc:
            await self._kill_switch.halt(f"reconciliation failed: {exc!r}")
            return

        new_positions = {p.symbol: p for p in self._tracker.positions()}
        new_orders = {o.oid: o for o in self._tracker.open_orders()}

        diffs = _diff_positions(old_positions, new_positions)
        diffs += _diff_orders(old_orders, new_orders)

        await self._journal.append(
            "reconciler_tick",
            {
                "old_positions": {s: str(p.size) for s, p in old_positions.items()},
                "new_positions": {s: str(p.size) for s, p in new_positions.items()},
                "old_order_oids": sorted(old_orders),
                "new_order_oids": sorted(new_orders),
                "divergences": diffs,
            },
        )

        if diffs:
            await self._kill_switch.halt(f"reconciliation divergence: {'; '.join(diffs)}")


def _diff_positions(old: dict[str, TrackedPosition], new: dict[str, TrackedPosition]) -> list[str]:
    out: list[str] = []
    for symbol in sorted(set(old) | set(new)):
        a = old.get(symbol)
        b = new.get(symbol)
        a_size = a.size if a else Decimal("0")
        b_size = b.size if b else Decimal("0")
        if a_size != b_size:
            out.append(f"position[{symbol}]: cached={a_size} truth={b_size}")
    return out


def _diff_orders(old: dict[int, TrackedOrder], new: dict[int, TrackedOrder]) -> list[str]:
    out: list[str] = []
    only_old = set(old) - set(new)
    only_new = set(new) - set(old)
    for oid in sorted(only_old):
        out.append(f"order[{oid}] disappeared: was {old[oid].symbol}")
    for oid in sorted(only_new):
        out.append(f"order[{oid}] appeared: now {new[oid].symbol}")
    return out
