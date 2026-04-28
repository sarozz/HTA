"""FastAPI app factory and ASGI entry point.

Two ways to run this:
  - In-process with the trading loop (the production path): create the
    bus first, hand the same instance to LiveSystem and to
    create_app(...).
  - Standalone for local UI development: `uvicorn
    src.dashboard_api.main:standalone_app` — starts an empty bus and
    serves a clean, journal-only view.

This module imports ONLY:
  - stdlib
  - fastapi / starlette
  - slowapi
  - src.dashboard_api.* (bus, auth, queries, routes, ws)
  - src.storage.journal (no keys, no SDK)

The dashboard-isolation test asserts that no forbidden modules
(SDK, exchange adapter, kill switch, order router, main.py) leak
into sys.modules at import time.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketException, status
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from src.dashboard_api import auth as auth_module
from src.dashboard_api.bus import Bus
from src.dashboard_api.queries import Queries
from src.dashboard_api.routes import build_router, limiter
from src.dashboard_api.ws import handle_ws

logger = logging.getLogger(__name__)

DEFAULT_JOURNAL_PATH = Path(os.environ.get("HTA_JOURNAL_PATH", "data/journal.sqlite"))


def create_app(
    *,
    journal_path: Path | str | None = None,
    bus: Bus | None = None,
) -> FastAPI:
    journal_path = Path(journal_path or DEFAULT_JOURNAL_PATH)
    bus = bus or Bus()
    queries = Queries(journal_path)

    app = FastAPI(
        title="HTA dashboard API",
        version="0.1.0",
        docs_url=None,  # disabled — read-only deployment, no public docs
        redoc_url=None,
        openapi_url=None,
    )

    app.state.bus = bus
    app.state.queries = queries
    app.state.journal_path = journal_path
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_exceeded(_request: Any, _exc: RateLimitExceeded) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "rate limit exceeded"},
        )

    app.add_middleware(SlowAPIMiddleware)
    app.include_router(build_router(queries))

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, token: str = Query(...)) -> None:
        try:
            auth_module.validate_token_or_raise(token)
        except auth_module.TokenError as exc:
            # Reject before accept — Starlette closes with 1008 by default
            # when WebSocketException is raised pre-accept.
            raise WebSocketException(code=1008, reason=str(exc)) from exc
        await handle_ws(websocket, app.state.bus, app.state.queries)

    @app.get("/")
    async def root() -> dict[str, Any]:
        # No bearer required on root; just a probe.
        return {"name": "hta-dashboard-api", "ok": True}

    return app


# Lazily-constructed standalone instance so `uvicorn src.dashboard_api.main:standalone_app`
# works without env-var fiddling beyond DASHBOARD_API_TOKEN + HTA_JOURNAL_PATH.
def _build_standalone() -> FastAPI:
    return create_app()


standalone_app = _build_standalone()


# Belt and suspenders: explicitly assert at import time that no forbidden
# modules slipped in via transitive imports. The isolation test re-runs
# this from a fresh interpreter; this raises if anything regressed.
def _assert_no_forbidden_imports() -> None:
    import sys

    forbidden = (
        "hyperliquid",
        "hyperliquid.exchange",
        "hyperliquid.info",
        "src.execution.exchange_adapter",
        "src.execution.order_router",
        "src.execution.position_tracker",
        "src.risk.kill_switch",
        "src.main",
    )
    leaked = [m for m in forbidden if m in sys.modules]
    if leaked:
        raise ImportError(
            f"dashboard_api imported forbidden modules: {leaked}. "
            "These hold keys or trade-side state and must stay out."
        )


# Skip the runtime check unless explicitly enabled; the isolation test
# performs the real check from a fresh interpreter where no other test
# has loaded the bot yet.
if os.environ.get("HTA_ASSERT_DASHBOARD_ISOLATION") == "1":
    _assert_no_forbidden_imports()
