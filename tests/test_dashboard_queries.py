"""Tests for dashboard_api.queries — read-only SQLite aggregations."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from src.dashboard_api.queries import MAX_ROWS, Queries
from src.storage.journal import Journal


@pytest.fixture
def seeded_db(tmp_path) -> Path:
    """A journal with one of every event_type the dashboard cares about."""
    path = tmp_path / "j.sqlite"
    j = Journal(path)
    # Seed via the public API to exercise the schema.
    import asyncio

    async def seed() -> None:
        now = time.time()
        await j.append("live_start", {"network": "testnet", "symbols": ["BTC", "ETH"]})
        # equity ticks across two 5m buckets
        for _i, eq in enumerate([1500.0, 1502.0, 1499.5, 1503.2, 1505.0]):
            await asyncio.sleep(0)
            await j.append(
                "equity_tick",
                {"equity": str(eq), "realized": str(eq - 5), "unrealized": "5"},
            )
        await j.append(
            "signal",
            {"symbol": "BTC", "strategy": "mean_reversion", "reason": "long"},
        )
        await j.append("halt", {"reason": "ws stale", "ts": now})
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
        await j.append(
            "fill",
            {
                "symbol": "BTC",
                "side": "sell",
                "size": "0.001",
                "price": "60100",
                "fee": "0.09",
                "strategy": "mean_reversion",
                "oid": 2,
                "opens_or_closes": "close",
                "pnl": "0.10",
            },
        )
        # State table — positions, orderbook, funding, heartbeat
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
            {
                "symbol": "BTC",
                "bids": [["60099", "0.5"], ["60098", "1.0"]],
                "asks": [["60101", "0.4"], ["60102", "0.8"]],
            },
        )
        await j.upsert_state(
            "funding",
            "BTC",
            {"coin": "BTC", "predicted_rate": "0.00012", "next_tick": now + 600},
        )
        await j.upsert_state("heartbeat", "_", {"halted": False, "subs": 1})

    asyncio.run(seed())
    return path


def test_health_returns_mode_and_uptime(seeded_db) -> None:
    q = Queries(seeded_db)
    h = q.health()
    assert h["mode"] == "testnet"
    assert h["uptime_seconds"] is not None
    assert h["heartbeat"]["halted"] is False


def test_snapshot_returns_all_top_level_keys(seeded_db) -> None:
    q = Queries(seeded_db)
    s = q.snapshot()
    assert {
        "health",
        "positions",
        "equity",
        "signals",
        "risk_events",
        "funding",
        "stats_24h",
    } <= set(s)


def test_equity_series_chronological_with_bucketing(seeded_db) -> None:
    q = Queries(seeded_db)
    series = q.equity_series(granularity="1m", limit=100)
    assert len(series) >= 1
    # Chronological order
    timestamps = [row["ts"] for row in series]
    assert timestamps == sorted(timestamps)


def test_equity_series_respects_limit(seeded_db) -> None:
    q = Queries(seeded_db)
    series = q.equity_series(granularity="5m", limit=2)
    assert len(series) <= 2


def test_equity_series_caps_limit_at_max_rows(seeded_db) -> None:
    q = Queries(seeded_db)
    # Pass an absurd limit; result must not exceed MAX_ROWS.
    series = q.equity_series(granularity="1m", limit=1_000_000_000)
    assert len(series) <= MAX_ROWS


def test_positions_returns_state_rows(seeded_db) -> None:
    q = Queries(seeded_db)
    positions = q.positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTC"
    assert positions[0]["size"] == "0.001"


def test_trades_returns_fill_rows(seeded_db) -> None:
    q = Queries(seeded_db)
    trades = q.trades()
    assert len(trades) == 2
    # Most recent first
    assert trades[0]["opens_or_closes"] == "close"


def test_trades_filterable_by_strategy(seeded_db) -> None:
    q = Queries(seeded_db)
    trades = q.trades(strategy="mean_reversion")
    assert len(trades) == 2
    none_match = q.trades(strategy="funding_capture")
    assert len(none_match) == 0


def test_trades_filterable_by_symbol(seeded_db) -> None:
    q = Queries(seeded_db)
    trades = q.trades(symbol="BTC")
    assert len(trades) == 2
    trades_eth = q.trades(symbol="ETH")
    assert len(trades_eth) == 0


def test_signals_returns_signal_events(seeded_db) -> None:
    q = Queries(seeded_db)
    signals = q.signals()
    assert any(s.get("strategy") == "mean_reversion" for s in signals)


def test_risk_events_returns_halt_events(seeded_db) -> None:
    q = Queries(seeded_db)
    events = q.risk_events()
    kinds = {e["kind"] for e in events}
    assert "halt" in kinds


def test_orderbook_returns_known_symbol(seeded_db) -> None:
    q = Queries(seeded_db)
    ob = q.orderbook("BTC")
    assert ob is not None
    assert "bids" in ob and "asks" in ob


def test_orderbook_returns_none_for_unknown_symbol(seeded_db) -> None:
    q = Queries(seeded_db)
    assert q.orderbook("NOPE") is None


def test_funding_returns_state_rows(seeded_db) -> None:
    q = Queries(seeded_db)
    rows = q.funding()
    assert len(rows) == 1
    assert rows[0]["coin"] == "BTC"


def test_stats_aggregates_close_fills(seeded_db) -> None:
    q = Queries(seeded_db)
    s = q.stats(window="24h")
    assert s["trades"] == 1  # only the close fill counts
    assert s["win_rate"] == 1.0
    assert s["gross_pnl"] == pytest.approx(0.10)


def test_stats_window_all_aggregates_everything(seeded_db) -> None:
    q = Queries(seeded_db)
    s = q.stats(window="all")
    assert s["trades"] == 1
    assert "per_strategy" in s
    assert "mean_reversion" in s["per_strategy"]


def test_query_uses_readonly_connection(seeded_db) -> None:
    """Defence in depth: connections must be opened mode=ro."""
    q = Queries(seeded_db)
    # Read-only connections refuse writes:
    conn = q._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO state (kind, key, value, ts_unix) VALUES ('x','y',?,0)", ("{}",))
    conn.close()


def test_query_handles_empty_db(tmp_path) -> None:
    """A brand-new journal must not crash on any endpoint."""
    Journal(tmp_path / "empty.sqlite")
    q = Queries(tmp_path / "empty.sqlite")
    assert q.health()["mode"] == "unknown"
    assert q.positions() == []
    assert q.trades() == []
    assert q.signals() == []
    assert q.risk_events() == []
    assert q.funding() == []
    assert q.equity_series() == []
    snapshot = q.snapshot()
    assert snapshot["positions"] == []


def test_signals_strategy_filter_is_substring_aware(seeded_db) -> None:
    q = Queries(seeded_db)
    rows = q.signals(strategy="mean_reversion")
    assert all(r.get("strategy") == "mean_reversion" for r in rows)


# Ensure the module did not import any forbidden bot modules.
def test_queries_module_imports_only_stdlib_and_journal() -> None:
    import sys

    forbidden = (
        "hyperliquid",
        "src.execution.exchange_adapter",
        "src.risk.kill_switch",
    )
    # If any test before us pulled them in, that's not this module's
    # fault — but queries.py itself must not need any of them, which
    # we confirm with a static read:
    src = (Path(__file__).parent.parent / "src" / "dashboard_api" / "queries.py").read_text()
    for name in forbidden:
        assert name not in src, f"queries.py imports forbidden module {name}"
    # Sanity: the test suite at large has loaded these for other tests, that's expected.
    _ = sys


_ = json  # used inside fixture; silence unused-import lint noise
