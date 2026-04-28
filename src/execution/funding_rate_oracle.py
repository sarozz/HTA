"""Polls Hyperliquid /info meta_and_asset_ctxs for current funding rates.

Hyperliquid pays funding hourly at the top of every UTC hour. The
`meta_and_asset_ctxs` endpoint returns the current funding rate per
asset, which is updated continuously and used by the protocol to
compute the actual payment when the tick fires.

We poll this endpoint periodically and cache the result so the
funding-capture scheduler can read fresh-enough data without making
its own network calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.strategies.funding_capture import FundingRateInfo

logger = logging.getLogger(__name__)


def _next_top_of_hour(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return nxt


class FundingRateOracle:
    """Polls meta_and_asset_ctxs and exposes a fresh dict of FundingRateInfo."""

    def __init__(self, info: Any, coins: list[str], poll_seconds: float = 30.0) -> None:
        self._info = info
        self._coins = list(coins)
        self._poll_seconds = poll_seconds
        self._stop_event = asyncio.Event()
        self._latest: dict[str, FundingRateInfo] = {}

    @property
    def latest(self) -> dict[str, FundingRateInfo]:
        return dict(self._latest)

    def stop(self) -> None:
        self._stop_event.set()

    async def refresh_once(self) -> dict[str, FundingRateInfo]:
        ctxs = await asyncio.to_thread(self._info.meta_and_asset_ctxs)
        # Shape: [meta, [ctx_for_asset_0, ctx_for_asset_1, ...]]
        # meta.universe[i].name corresponds to ctxs[i]
        try:
            universe = ctxs[0].get("universe", []) if isinstance(ctxs, (list, tuple)) else []
            asset_ctxs = ctxs[1] if isinstance(ctxs, (list, tuple)) and len(ctxs) > 1 else []
        except (IndexError, AttributeError, TypeError):
            return self._latest

        now = datetime.now(timezone.utc)
        next_tick = _next_top_of_hour(now)
        out: dict[str, FundingRateInfo] = {}
        for asset, ctx in zip(universe, asset_ctxs, strict=False):
            if not isinstance(asset, dict) or not isinstance(ctx, dict):
                continue
            name = asset.get("name")
            if name not in self._coins:
                continue
            try:
                rate = Decimal(str(ctx.get("funding", "0")))
            except (ValueError, ArithmeticError):
                continue
            out[name] = FundingRateInfo(
                coin=name, predicted_rate=rate, next_tick_time=next_tick, as_of=now
            )
        self._latest = out
        return out

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("funding rate oracle refresh failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
                return
            except asyncio.TimeoutError:
                continue
