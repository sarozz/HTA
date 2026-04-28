"""REST endpoints. GET only; bearer-token auth on every route.

Hard 1000-row cap on every list endpoint regardless of params (the
query layer enforces it too — defence in depth).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.dashboard_api.auth import require_token
from src.dashboard_api.queries import (
    EQUITY_GRANULARITY_SECONDS,
    MAX_ROWS,
    ROLLING_WINDOWS_SECONDS,
    Queries,
)

# Rate limit applied via slowapi at the app level. The limiter is created in
# main.py and its decorator is wired onto these routes there. We expose the
# constants here for visibility.
RATE_LIMIT = "60/minute"

limiter = Limiter(key_func=get_remote_address)


def build_router(queries: Queries) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/health")
    @limiter.limit(RATE_LIMIT)
    async def health(request: Request) -> dict[str, Any]:
        _ = request  # rate limiter needs the request arg
        return queries.health()

    @router.get("/snapshot")
    @limiter.limit(RATE_LIMIT)
    async def snapshot(request: Request) -> dict[str, Any]:
        _ = request
        return queries.snapshot()

    @router.get("/equity")
    @limiter.limit(RATE_LIMIT)
    async def equity(
        request: Request,
        granularity: str = Query("5m"),
        since: float | None = Query(None),
        limit: int = Query(MAX_ROWS, le=MAX_ROWS),
    ) -> list[dict[str, Any]]:
        _ = request
        if granularity not in EQUITY_GRANULARITY_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"granularity must be one of {sorted(EQUITY_GRANULARITY_SECONDS)}",
            )
        return queries.equity_series(granularity=granularity, since=since, limit=limit)

    @router.get("/positions")
    @limiter.limit(RATE_LIMIT)
    async def positions(request: Request) -> list[dict[str, Any]]:
        _ = request
        return queries.positions()

    @router.get("/trades")
    @limiter.limit(RATE_LIMIT)
    async def trades(
        request: Request,
        strategy: str | None = Query(None),
        symbol: str | None = Query(None),
        limit: int = Query(MAX_ROWS, le=MAX_ROWS),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        _ = request
        return queries.trades(strategy=strategy, symbol=symbol, limit=limit, offset=offset)

    @router.get("/signals")
    @limiter.limit(RATE_LIMIT)
    async def signals(
        request: Request,
        strategy: str | None = Query(None),
        symbol: str | None = Query(None),
        limit: int = Query(MAX_ROWS, le=MAX_ROWS),
    ) -> list[dict[str, Any]]:
        _ = request
        return queries.signals(strategy=strategy, symbol=symbol, limit=limit)

    @router.get("/risk_events")
    @limiter.limit(RATE_LIMIT)
    async def risk_events(
        request: Request, limit: int = Query(MAX_ROWS, le=MAX_ROWS)
    ) -> list[dict[str, Any]]:
        _ = request
        return queries.risk_events(limit=limit)

    @router.get("/orderbook/{symbol}")
    @limiter.limit(RATE_LIMIT)
    async def orderbook(request: Request, symbol: str) -> dict[str, Any]:
        _ = request
        ob = queries.orderbook(symbol)
        if ob is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no orderbook for {symbol}",
            )
        return ob

    @router.get("/funding")
    @limiter.limit(RATE_LIMIT)
    async def funding(request: Request) -> list[dict[str, Any]]:
        _ = request
        return queries.funding()

    @router.get("/stats")
    @limiter.limit(RATE_LIMIT)
    async def stats(request: Request, window: str = Query("24h")) -> dict[str, Any]:
        _ = request
        if window not in ROLLING_WINDOWS_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"window must be one of {sorted(ROLLING_WINDOWS_SECONDS)}",
            )
        return queries.stats(window=window)

    return router
