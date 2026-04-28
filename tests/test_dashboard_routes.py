"""HTTP endpoint tests for dashboard_api.

Drives a real FastAPI app with the fastapi.testclient — no network. The
journal SQLite is seeded per-test from the same fixture used by
test_dashboard_queries.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard_api.auth import ENV_VAR
from src.dashboard_api.main import create_app
from src.storage.journal import Journal

TOKEN = "test-token-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, TOKEN)


@pytest.fixture
def seeded_app(tmp_path) -> tuple[TestClient, Path]:
    path = tmp_path / "j.sqlite"
    j = Journal(path)

    async def seed() -> None:
        await j.append("live_start", {"network": "testnet", "symbols": ["BTC"]})
        for eq in (1500.0, 1502.0, 1505.0):
            await j.append(
                "equity_tick",
                {"equity": str(eq), "realized": str(eq - 5), "unrealized": "5"},
            )
        await j.append("signal", {"symbol": "BTC", "strategy": "mean_reversion", "reason": "long"})
        await j.append("halt", {"reason": "test halt"})
        await j.append(
            "fill",
            {
                "symbol": "BTC",
                "side": "buy",
                "size": "0.001",
                "price": "60000",
                "fee": "0.09",
                "strategy": "mean_reversion",
                "oid": 1,
                "opens_or_closes": "open",
            },
        )
        await j.upsert_state(
            "position",
            "BTC",
            {
                "symbol": "BTC",
                "size": "0.001",
                "entry_price": "60000",
                "mark_price": "60100",
                "liq_price": None,
            },
        )
        await j.upsert_state(
            "orderbook",
            "BTC",
            {"symbol": "BTC", "bids": [["60099", "0.5"]], "asks": [["60101", "0.4"]]},
        )
        await j.upsert_state(
            "funding",
            "BTC",
            {"coin": "BTC", "predicted_rate": "0.00012", "next_tick": time.time() + 600},
        )
        await j.upsert_state("heartbeat", "_", {"halted": False, "subs": 0})

    asyncio.run(seed())
    app = create_app(journal_path=path)
    return TestClient(app), path


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_endpoints_require_bearer_token(seeded_app) -> None:
    client, _ = seeded_app
    for path in (
        "/api/health",
        "/api/snapshot",
        "/api/equity",
        "/api/positions",
        "/api/trades",
        "/api/signals",
        "/api/risk_events",
        "/api/orderbook/BTC",
        "/api/funding",
        "/api/stats",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should require auth"


def test_endpoints_reject_wrong_bearer(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_root_does_not_require_auth(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["name"] == "hta-dashboard-api"


# ---------------------------------------------------------------------------
# Read-only contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/health"),
        ("put", "/api/health"),
        ("patch", "/api/health"),
        ("delete", "/api/health"),
        ("post", "/api/snapshot"),
        ("post", "/api/positions"),
    ],
)
def test_mutating_methods_rejected(seeded_app, method, path) -> None:
    client, _ = seeded_app
    r = getattr(client, method)(path, headers=_auth())
    assert r.status_code in (
        404,
        405,
    ), f"{method.upper()} {path} should be rejected (405) or not exist (404), got {r.status_code}"


# ---------------------------------------------------------------------------
# Endpoint payloads
# ---------------------------------------------------------------------------


def test_health_returns_mode(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/health", headers=_auth())
    assert r.status_code == 200
    assert r.json()["mode"] == "testnet"


def test_snapshot_returns_full_payload(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/snapshot", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert {
        "health",
        "positions",
        "equity",
        "signals",
        "risk_events",
        "funding",
        "stats_24h",
    } <= set(body)


def test_equity_default_granularity_returns_data(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/equity", headers=_auth())
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_equity_invalid_granularity_400(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/equity?granularity=2y", headers=_auth())
    assert r.status_code == 400


def test_positions_returns_list(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/positions", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(p["symbol"] == "BTC" for p in body)


def test_trades_filters_by_strategy(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/trades?strategy=mean_reversion", headers=_auth())
    assert r.status_code == 200
    rows = r.json()
    assert all(t["strategy"] == "mean_reversion" for t in rows)


def test_signals_returns_list(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/signals", headers=_auth())
    assert r.status_code == 200
    assert any(s.get("strategy") == "mean_reversion" for s in r.json())


def test_risk_events_returns_halt_event(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/risk_events", headers=_auth())
    assert r.status_code == 200
    assert any(e["kind"] == "halt" for e in r.json())


def test_orderbook_returns_known_symbol(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/orderbook/BTC", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "bids" in body and "asks" in body


def test_orderbook_unknown_symbol_404(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/orderbook/NOPE", headers=_auth())
    assert r.status_code == 404


def test_funding_returns_known_coins(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/funding", headers=_auth())
    assert r.status_code == 200
    rows = r.json()
    assert any(row["coin"] == "BTC" for row in rows)


def test_stats_default_window(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/stats", headers=_auth())
    assert r.status_code == 200
    assert r.json()["window"] == "24h"


def test_stats_invalid_window_400(seeded_app) -> None:
    client, _ = seeded_app
    r = client.get("/api/stats?window=99y", headers=_auth())
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Hard 1000-row cap
# ---------------------------------------------------------------------------


def test_limit_param_caps_at_1000(seeded_app) -> None:
    client, _ = seeded_app
    # FastAPI rejects limit > 1000 (le=MAX_ROWS validator) with 422.
    r = client.get("/api/trades?limit=999999", headers=_auth())
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI is disabled
# ---------------------------------------------------------------------------


def test_openapi_disabled(seeded_app) -> None:
    client, _ = seeded_app
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


_ = pytest
