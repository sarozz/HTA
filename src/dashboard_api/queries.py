"""Read-only SQLite queries powering the REST endpoints.

Talks directly to the journal SQLite file (events + state tables). The
trading process and the dashboard process share the same DB file via
WAL mode. Every list query is hard-capped at MAX_ROWS (1000) regardless
of input parameters — the route layer also applies the cap, but
defending it here means a buggy route can't accidentally slurp the
whole journal.

This module imports stdlib + sqlite3 only. No SDK, no order router, no
keys. The dashboard_api isolation test asserts that.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_ROWS = 1000

# Event types that count as "risk events" for /api/risk_events.
RISK_EVENT_TYPES = (
    "halt",
    "resume",
    "order_blocked",
    "order_rejected_by_risk",
    "order_abandoned",
    "reconciler_tick",
    "ws_disconnected",
    "signal_suppressed",
)

# Default windows for /api/stats.
ROLLING_WINDOWS_SECONDS = {
    "1h": 3600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
    "all": None,
}

# Granularity bucket sizes (in seconds) for /api/equity.
EQUITY_GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "1h": 3600,
}


class Queries:
    """Thin handle over a journal SQLite file. Every method is read-only."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        # uri=true + mode=ro to fail loudly if anything slips a write through.
        # The DB lives in WAL mode (set by Journal); RO connections can read
        # cleanly while the writer process is appending.
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # /api/health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            heartbeat = conn.execute(
                "SELECT value, ts_unix FROM state WHERE kind='heartbeat' AND key='_'"
            ).fetchone()
            last_tick_row = conn.execute(
                "SELECT MAX(ts_unix) FROM events WHERE event_type IN ('signal','equity_tick','tick','reconciler_tick')"
            ).fetchone()
            start_row = conn.execute(
                "SELECT ts_unix, payload FROM events WHERE event_type='live_start' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if heartbeat is None:
            beat = {"ts": None}
        else:
            beat = json.loads(heartbeat["value"])
            beat["ts"] = heartbeat["ts_unix"]
        last_tick = last_tick_row[0] if last_tick_row and last_tick_row[0] else None
        if start_row is not None:
            start_payload = json.loads(start_row["payload"])
            mode = start_payload.get("network", "unknown")
            session_started_at = start_row["ts_unix"]
        else:
            mode = "unknown"
            session_started_at = None
        uptime = time.time() - session_started_at if session_started_at is not None else None
        return {
            "heartbeat": beat,
            "last_tick_ts": last_tick,
            "session_started_at": session_started_at,
            "uptime_seconds": uptime,
            "mode": mode,
        }

    # ------------------------------------------------------------------
    # /api/snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "health": self.health(),
            "positions": self.positions(),
            "equity": self.equity_series("5m", since=None, limit=288),  # ~24h
            "signals": self.signals(limit=50),
            "risk_events": self.risk_events(limit=50),
            "funding": self.funding(),
            "stats_24h": self.stats("24h"),
        }

    # ------------------------------------------------------------------
    # /api/equity
    # ------------------------------------------------------------------

    def equity_series(
        self,
        granularity: str = "5m",
        since: float | None = None,
        limit: int = MAX_ROWS,
    ) -> list[dict[str, Any]]:
        bucket = EQUITY_GRANULARITY_SECONDS.get(granularity, 300)
        limit = _cap(limit)
        params: list[Any] = []
        where = "event_type='equity_tick'"
        if since is not None:
            where += " AND ts_unix >= ?"
            params.append(since)
        # Bucket by floor(ts/bucket) and take the last point per bucket.
        sql = f"""
        SELECT ts_unix, payload FROM events
        WHERE {where}
        ORDER BY ts_unix DESC
        LIMIT ?
        """
        params.append(limit * 4 if bucket > 60 else limit)  # over-fetch then bucket
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        seen_buckets: set[int] = set()
        for r in rows:
            b = int(r["ts_unix"] // bucket)
            if b in seen_buckets:
                continue
            seen_buckets.add(b)
            payload = json.loads(r["payload"])
            out.append(
                {
                    "ts": r["ts_unix"],
                    "equity": payload.get("equity"),
                    "realized": payload.get("realized"),
                    "unrealized": payload.get("unrealized"),
                }
            )
            if len(out) >= limit:
                break
        out.reverse()  # chronological
        return out

    # ------------------------------------------------------------------
    # /api/positions
    # ------------------------------------------------------------------

    def positions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, ts_unix FROM state WHERE kind='position' "
                "ORDER BY key LIMIT ?",
                (MAX_ROWS,),
            ).fetchall()
        return [_state_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # /api/trades
    # ------------------------------------------------------------------

    def trades(
        self,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int = MAX_ROWS,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Closed trades reconstructed from `fill` events.

        Each fill event payload should carry: symbol, side, size, price,
        fee, strategy, oid, opens_or_closes ('open' | 'close'), and on
        a 'close' fill the trade's pnl + entry data.
        """
        limit = _cap(limit)
        clauses: list[str] = ["event_type='fill'"]
        params: list[Any] = []
        if strategy:
            clauses.append("payload LIKE ?")
            params.append(f'%"strategy": "{strategy}"%')
        if symbol:
            clauses.append("payload LIKE ?")
            params.append(f'%"symbol": "{symbol}"%')
        sql = f"""
        SELECT ts_unix, payload FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY ts_unix DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"ts": r["ts_unix"], **json.loads(r["payload"])} for r in rows]

    # ------------------------------------------------------------------
    # /api/signals
    # ------------------------------------------------------------------

    def signals(
        self,
        strategy: str | None = None,
        symbol: str | None = None,
        limit: int = MAX_ROWS,
    ) -> list[dict[str, Any]]:
        limit = _cap(limit)
        clauses = ["event_type IN ('signal','funding_capture_decision')"]
        params: list[Any] = []
        if strategy:
            clauses.append("payload LIKE ?")
            params.append(f'%"strategy": "{strategy}"%')
        if symbol:
            clauses.append("payload LIKE ?")
            params.append(f'%"symbol": "{symbol}"%')
        sql = f"""
        SELECT ts_unix, event_type, payload FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY ts_unix DESC
        LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "ts": r["ts_unix"],
                "event_type": r["event_type"],
                **json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # /api/risk_events
    # ------------------------------------------------------------------

    def risk_events(self, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
        limit = _cap(limit)
        placeholders = ",".join("?" for _ in RISK_EVENT_TYPES)
        sql = f"""
        SELECT ts_unix, event_type, payload FROM events
        WHERE event_type IN ({placeholders})
        ORDER BY ts_unix DESC
        LIMIT ?
        """
        params = [*list(RISK_EVENT_TYPES), limit]
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "ts": r["ts_unix"],
                "kind": r["event_type"],
                **json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # /api/orderbook/:symbol
    # ------------------------------------------------------------------

    def orderbook(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value, ts_unix FROM state WHERE kind='orderbook' AND key=?",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        return _state_row_to_dict(row)

    # ------------------------------------------------------------------
    # /api/funding
    # ------------------------------------------------------------------

    def funding(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, ts_unix FROM state WHERE kind='funding' ORDER BY key"
            ).fetchall()
        return [_state_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # /api/stats
    # ------------------------------------------------------------------

    def stats(self, window: str = "24h") -> dict[str, Any]:
        seconds = ROLLING_WINDOWS_SECONDS.get(window)
        cutoff = (time.time() - seconds) if seconds is not None else None

        params: list[Any] = []
        ts_clause = ""
        if cutoff is not None:
            ts_clause = "AND ts_unix >= ?"
            params.insert(0, cutoff)

        with self._connect() as conn:
            fills = conn.execute(
                f"SELECT payload FROM events WHERE event_type='fill' {ts_clause}",
                params,
            ).fetchall()

        wins = 0
        losses = 0
        gross_pnl = 0.0
        fees = 0.0
        best = float("-inf")
        worst = float("inf")
        per_strategy: dict[str, dict[str, float]] = {}
        for r in fills:
            p = json.loads(r["payload"])
            if p.get("opens_or_closes") != "close":
                continue
            pnl = float(p.get("pnl", 0.0))
            fee = float(p.get("fee", 0.0))
            gross_pnl += pnl
            fees += fee
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            best = max(best, pnl)
            worst = min(worst, pnl)
            strat = p.get("strategy", "unknown")
            bucket = per_strategy.setdefault(strat, {"trades": 0, "pnl": 0.0, "fees": 0.0})
            bucket["trades"] += 1
            bucket["pnl"] += pnl
            bucket["fees"] += fee

        total = wins + losses
        win_rate = wins / total if total > 0 else 0.0
        profits = sum(
            float(json.loads(r["payload"]).get("pnl", 0.0))
            for r in fills
            if json.loads(r["payload"]).get("opens_or_closes") == "close"
            and float(json.loads(r["payload"]).get("pnl", 0.0)) > 0
        )
        loss_sum = -sum(
            float(json.loads(r["payload"]).get("pnl", 0.0))
            for r in fills
            if json.loads(r["payload"]).get("opens_or_closes") == "close"
            and float(json.loads(r["payload"]).get("pnl", 0.0)) < 0
        )
        profit_factor = (profits / loss_sum) if loss_sum > 0 else None
        return {
            "window": window,
            "trades": total,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": gross_pnl - fees,
            "best_trade": best if best != float("-inf") else None,
            "worst_trade": worst if worst != float("inf") else None,
            "per_strategy": per_strategy,
        }


def _state_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"] if "key" in row.keys() else None,
        "ts": row["ts_unix"],
        **json.loads(row["value"]),
    }


def _cap(n: int) -> int:
    if n is None or n < 1:
        return 1
    return min(n, MAX_ROWS)


def all_event_types(conn: sqlite3.Connection) -> Iterable[str]:
    """Helper for diagnostics."""
    return (r[0] for r in conn.execute("SELECT DISTINCT event_type FROM events"))
