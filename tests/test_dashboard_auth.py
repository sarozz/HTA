"""Tests for dashboard_api.auth — bearer token + WS query param."""

from __future__ import annotations

import pytest

from src.dashboard_api.auth import (
    ENV_VAR,
    TokenError,
    constant_time_match,
    get_expected_token,
    require_token,
    validate_token_or_raise,
)


def test_constant_time_match_accepts_identical_strings() -> None:
    assert constant_time_match("hunter2", "hunter2") is True


def test_constant_time_match_rejects_different_strings() -> None:
    assert constant_time_match("hunter2", "wrong") is False


def test_constant_time_match_rejects_empty_expected() -> None:
    assert constant_time_match("hunter2", "") is False


def test_constant_time_match_rejects_empty_provided() -> None:
    assert constant_time_match("", "hunter2") is False


def test_get_expected_token_reads_env(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "tok-123")
    assert get_expected_token() == "tok-123"


def test_get_expected_token_returns_empty_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert get_expected_token() == ""


def test_validate_token_or_raise_accepts_correct(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    validate_token_or_raise("good-token")  # no raise


def test_validate_token_or_raise_rejects_incorrect(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    with pytest.raises(TokenError):
        validate_token_or_raise("bad-token")


def test_validate_token_or_raise_rejects_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(TokenError):
        validate_token_or_raise("anything")


def test_validate_token_or_raise_rejects_none(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    with pytest.raises(TokenError):
        validate_token_or_raise(None)


# ---------------------------------------------------------------------------
# require_token (FastAPI dep)
# ---------------------------------------------------------------------------


async def test_require_token_accepts_correct_bearer(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    await require_token(authorization="Bearer good-token")  # no raise


async def test_require_token_rejects_missing_header(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await require_token(authorization=None)
    assert exc.value.status_code == 401


async def test_require_token_rejects_wrong_scheme(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await require_token(authorization="Basic dXNlcjpwYXNz")
    assert exc.value.status_code == 401


async def test_require_token_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setenv(ENV_VAR, "good-token")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await require_token(authorization="Bearer bad-token")
    assert exc.value.status_code == 401


_ = pytest
