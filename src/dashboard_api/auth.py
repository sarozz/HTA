"""Bearer-token auth for the dashboard API.

Single static token sourced from DASHBOARD_API_TOKEN. Compared with
secrets.compare_digest to avoid timing attacks. There are no user
accounts and no rotation flow — operator-only deployment.

REST: validated via the FastAPI dependency `require_token`.
WS:   validated via `validate_token_or_raise` before the upgrade is
      accepted (FastAPI/Starlette has no header-level dep on websocket
      handshake, so the ws handler calls this manually).
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

ENV_VAR = "DASHBOARD_API_TOKEN"
_BEARER_PREFIX = "Bearer "


class TokenError(Exception):
    pass


def get_expected_token() -> str:
    token = os.environ.get(ENV_VAR, "")
    if not token:
        # Fail closed: no env var means every request is rejected.
        return ""
    return token


def constant_time_match(provided: str, expected: str) -> bool:
    if not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def validate_token_or_raise(provided: str | None) -> None:
    """Raise TokenError if `provided` does not match the expected token."""
    if provided is None or not constant_time_match(provided, get_expected_token()):
        raise TokenError("invalid or missing token")


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency for REST routes."""
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len(_BEARER_PREFIX) :]
    if not constant_time_match(token, get_expected_token()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
