"""Minimal append-only SQLite journal.

Every row is a JSON blob. The journal is the system of record for orders,
fills, signals, halts, errors, and outbound notifications.

Writes happen in a worker thread (sqlite3 is sync) so the event loop never
blocks on disk I/O.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix    REAL    NOT NULL,
    event_type TEXT    NOT NULL,
    payload    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts_unix);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""


class JournalLike(Protocol):
    """Anything the rest of the system needs from the journal."""

    async def append(self, event_type: str, payload: dict[str, Any]) -> None: ...


class Journal:
    """SQLite-backed JSON journal."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        ts = time.time()
        blob = json.dumps(payload, default=str, sort_keys=True)
        async with self._lock:
            await asyncio.to_thread(self._write, ts, event_type, blob)

    def _write(self, ts: float, event_type: str, blob: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts_unix, event_type, payload) VALUES (?, ?, ?)",
                (ts, event_type, blob),
            )


class InMemoryJournal:
    """Test double. Stores events in a list; same async interface."""

    def __init__(self) -> None:
        self.events: list[tuple[float, str, dict[str, Any]]] = []

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((time.time(), event_type, dict(payload)))
