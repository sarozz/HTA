"""Local mock of the HTA dashboard API.

Mirrors the real bot API's REST endpoints with synthetic data so the
Next.js dashboard can be developed and demoed offline.

Run:
    DASHBOARD_API_TOKEN=local-dev-token-do-not-use-in-prod \\
      uvicorn mock_api.server:app --host 127.0.0.1 --port 8080

Or via docker-compose (preferred): `docker compose up`.

The mock walks a small synthetic equity series and rotates through a
handful of canned signals / trades / risk events to make the UI feel
alive. Bearer auth is the same as the real API (DASHBOARD_API_TOKEN).
"""

from __future__ import annotations

import math
import os
import random
import secrets
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status

TOKEN_ENV = "DASHBOARD_API_TOKEN"
DEFAULT_TOKEN = "local-dev-token-do-not-use-in-prod"

app = FastAPI(title="HTA mock API", openapi_url=None, docs_url=None)

START_TS = time.time() - 24 * 3600
START_EQUITY = 1500.0


def _expected_token() -> str:
    return os.environ.get(TOKEN_ENV) or DEFAULT_TOKEN


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    given = authorization[len("Bearer ") :]
    if not secrets.compare_digest(given, _expected_token()):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# Synthetic data generators
# ---------------------------------------------------------------------------


def _equity_series(granularity: str = "5m", n: int = 288) -> list[dict[str, Any]]:
    bucket = {"1m": 60, "5m": 300, "1h": 3600}.get(granularity, 300)
    out: list[dict[str, Any]] = []
    eq = START_EQUITY
    rng = random.Random(7)
    now = time.time()
    for i in range(n):
        ts = now - (n - i) * bucket
        # Slight drift + noise; small mean-reverting positive bias.
        eq += rng.gauss(0.05, 0.6)
        out.append(
            {
                "ts": ts,
                "equity": f"{eq:.2f}",
                "realized": f"{eq - 5:.2f}",
                "unrealized": "5.00",
            }
        )
    return out


def _positions() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "symbol": "BTC",
            "size": "0.0024",
            "entry_price": "60500.00",
            "mark_price": "60624.50",
            "liq_price": "55480.00",
            "ts": now - 1800,
        },
    ]


def _signals(n: int = 50) -> list[dict[str, Any]]:
    rng = random.Random(11)
    coins = ["BTC", "ETH", "SOL"]
    strategies = ["mean_reversion", "funding_capture"]
    reasons = [
        "long z=-2.31 rsi=27.1",
        "short z=2.04 rsi=72.4",
        "no signal z=-0.42 rsi=48.0",
        "target z=0.05 bars=3",
        "time_stop bars_held=5",
        "stop |z|=3.71",
        "funding entry T-600s",
        "scheduled exit T+72s",
    ]
    out = []
    now = time.time()
    for i in range(n):
        out.append(
            {
                "ts": now - i * 60 - rng.randint(0, 30),
                "event_type": "signal",
                "symbol": rng.choice(coins),
                "strategy": rng.choice(strategies),
                "reason": rng.choice(reasons),
                "target_notional": f"{rng.uniform(100, 700):.2f}",
            }
        )
    return out


def _risk_events() -> list[dict[str, Any]]:
    now = time.time()
    return [
        {
            "ts": now - 120,
            "kind": "order_blocked",
            "symbol": "BTC",
            "reason": "max_open_orders cap reached",
        },
        {
            "ts": now - 500,
            "kind": "reconciler_tick",
            "reason": "clean tick · no divergence",
        },
        {
            "ts": now - 8000,
            "kind": "order_abandoned",
            "symbol": "ETH",
            "reason": "ALO would_take · retry exhausted",
        },
    ]


def _funding() -> list[dict[str, Any]]:
    now = time.time()
    nxt = math.ceil(now / 3600) * 3600
    return [
        {"coin": "BTC", "predicted_rate": "0.00012", "next_tick": nxt, "ts": now},
        {"coin": "ETH", "predicted_rate": "0.00009", "next_tick": nxt, "ts": now},
        {"coin": "SOL", "predicted_rate": "0.00021", "next_tick": nxt, "ts": now},
    ]


def _trades(n: int = 80) -> list[dict[str, Any]]:
    rng = random.Random(13)
    out = []
    now = time.time()
    for i in range(n):
        side = rng.choice(["buy", "sell"])
        opens_or_closes = "close" if i % 2 else "open"
        out.append(
            {
                "ts": now - i * 600,
                "symbol": rng.choice(["BTC", "ETH", "SOL"]),
                "side": side,
                "size": f"{rng.uniform(0.001, 0.01):.6f}",
                "price": f"{rng.uniform(50, 70000):.2f}",
                "fee": f"{rng.uniform(0.05, 0.5):.2f}",
                "strategy": rng.choice(["mean_reversion", "funding_capture"]),
                "oid": 1_000_000 + i,
                "opens_or_closes": opens_or_closes,
                "pnl": f"{rng.gauss(0.5, 1.0):.2f}" if opens_or_closes == "close" else None,
            }
        )
    return out


def _stats(window: str = "24h") -> dict[str, Any]:
    rng = random.Random(window)
    trades = rng.randint(10, 60)
    wins = rng.randint(int(trades * 0.4), int(trades * 0.8))
    gross = rng.gauss(8.0, 12.0)
    fees = abs(gross) * 0.4
    return {
        "window": window,
        "trades": trades,
        "win_rate": wins / trades if trades > 0 else 0,
        "profit_factor": rng.uniform(0.8, 1.6),
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": gross - fees,
        "best_trade": rng.uniform(1.0, 8.0),
        "worst_trade": rng.uniform(-6.0, -0.5),
        "per_strategy": {
            "mean_reversion": {"trades": int(trades * 0.7), "pnl": gross * 0.6, "fees": fees * 0.6},
            "funding_capture": {"trades": int(trades * 0.3), "pnl": gross * 0.4, "fees": fees * 0.4},
        },
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
def root() -> dict[str, Any]:
    return {"name": "hta-dashboard-api-mock", "ok": True}


@app.get("/api/health")
def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_auth(authorization)
    now = time.time()
    return {
        "heartbeat": {"ts": now, "halted": False, "subs": 0},
        "last_tick_ts": now - 2,
        "session_started_at": START_TS,
        "uptime_seconds": now - START_TS,
        "mode": "testnet",
    }


@app.get("/api/snapshot")
def snapshot(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_auth(authorization)
    return {
        "health": health(authorization),
        "positions": _positions(),
        "equity": _equity_series("5m", 288),
        "signals": _signals(50),
        "risk_events": _risk_events(),
        "funding": _funding(),
        "stats_24h": _stats("24h"),
    }


@app.get("/api/equity")
def equity(
    authorization: str | None = Header(default=None),
    granularity: str = "5m",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    _check_auth(authorization)
    if granularity not in ("1m", "5m", "1h"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad granularity")
    return _equity_series(granularity, min(limit, 1000))


@app.get("/api/positions")
def positions(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _check_auth(authorization)
    return _positions()


@app.get("/api/trades")
def trades(
    authorization: str | None = Header(default=None),
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    _check_auth(authorization)
    rows = _trades(80)
    if strategy:
        rows = [t for t in rows if t["strategy"] == strategy]
    if symbol:
        rows = [t for t in rows if t["symbol"] == symbol]
    return rows[: min(limit, 1000)]


@app.get("/api/signals")
def signals(
    authorization: str | None = Header(default=None),
    limit: int = 200,
) -> list[dict[str, Any]]:
    _check_auth(authorization)
    return _signals(min(limit, 1000))


@app.get("/api/risk_events")
def risk_events(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _check_auth(authorization)
    return _risk_events()


@app.get("/api/funding")
def funding(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _check_auth(authorization)
    return _funding()


@app.get("/api/stats")
def stats(
    authorization: str | None = Header(default=None),
    window: str = "24h",
) -> dict[str, Any]:
    _check_auth(authorization)
    if window not in ("1h", "24h", "7d", "30d", "all"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad window")
    return _stats(window)


@app.get("/api/orderbook/{symbol}")
def orderbook(
    symbol: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _check_auth(authorization)
    rng = random.Random(symbol)
    mid = 60500.0 if symbol == "BTC" else 3000.0 if symbol == "ETH" else 140.0
    bids = [(mid - i * 0.5, rng.uniform(0.1, 1.5)) for i in range(20)]
    asks = [(mid + i * 0.5, rng.uniform(0.1, 1.5)) for i in range(20)]
    return {
        "symbol": symbol,
        "bids": [[f"{p:.2f}", f"{q:.4f}"] for p, q in bids],
        "asks": [[f"{p:.2f}", f"{q:.4f}"] for p, q in asks],
        "ts": time.time(),
    }
