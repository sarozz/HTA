"""Sanity-check Hyperliquid testnet connectivity.

Reads HL_API_KEY and HL_ACCOUNT_ADDRESS from .env, connects to the
Hyperliquid testnet, and prints a short report:
  - account balance (margin summary accountValue)
  - first 5 perps in the universe
  - current BTC-PERP mid-price
  - next funding tick (Hyperliquid pays hourly at top of UTC hour)

Read-only; no orders are placed. Always uses testnet regardless of env.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

REQUIRED_ENV_VARS = ("HL_API_KEY", "HL_ACCOUNT_ADDRESS")


def load_env() -> dict[str, str]:
    """Load required env vars; raise SystemExit with a clear message if missing."""
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    missing = [k for k in REQUIRED_ENV_VARS if not os.environ.get(k)]
    if missing:
        print(
            "ERROR: missing required environment variable(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in your API wallet key and account address.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return {k: os.environ[k] for k in REQUIRED_ENV_VARS}


def next_funding_time(now: datetime | None = None) -> datetime:
    """Hyperliquid pays funding at the top of every UTC hour."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def account_value(user_state: dict[str, Any]) -> str:
    margin = user_state.get("marginSummary") or {}
    return str(margin.get("accountValue", "unknown"))


def main() -> int:
    env = load_env()
    address = env["HL_ACCOUNT_ADDRESS"]

    # Hard-coded testnet per CLAUDE.md non-negotiable #1.
    info = Info(constants.TESTNET_API_URL, skip_ws=True)

    user_state = info.user_state(address)
    meta = info.meta()
    mids = info.all_mids()

    universe = meta.get("universe", [])
    first_five = [a.get("name", "?") for a in universe[:5]]
    btc_mid = mids.get("BTC")

    print("Hyperliquid testnet connection OK")
    print(f"  endpoint         : {constants.TESTNET_API_URL}")
    print(f"  account          : {address}")
    print(f"  account balance  : {account_value(user_state)} USDC")
    print(f"  first 5 perps    : {', '.join(first_five) if first_five else '(none)'}")
    print(f"  BTC-PERP mid-px  : {btc_mid if btc_mid is not None else '(unavailable)'}")
    print(f"  next funding     : {next_funding_time().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
