"""Tests for scripts/check_connection.py.

The SDK is mocked; no network is touched.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_connection.py"


def _load_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import scripts/check_connection.py as a module with the SDK mocked.

    The Info constructor in the real SDK calls spot_meta() over the network on
    init, so we must replace it before import.
    """
    fake_info_cls = MagicMock(name="Info")
    fake_constants = MagicMock(name="constants")
    fake_constants.TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"

    # Patch the modules the script imports from.
    sys.modules["hyperliquid"] = MagicMock()
    fake_info_module = MagicMock()
    fake_info_module.Info = fake_info_cls
    sys.modules["hyperliquid.info"] = fake_info_module
    fake_utils_module = MagicMock()
    fake_utils_module.constants = fake_constants
    sys.modules["hyperliquid.utils"] = fake_utils_module

    spec = importlib.util.spec_from_file_location("check_connection", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._fake_info_cls = fake_info_cls  # expose for assertions
    return module


def test_exits_cleanly_when_keys_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HL_API_KEY", raising=False)
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    # Prevent .env from leaking real values into the test.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)

    module = _load_script(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "missing required environment variable" in err
    assert "HL_API_KEY" in err
    assert "HL_ACCOUNT_ADDRESS" in err


def test_exits_cleanly_when_only_one_key_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HL_API_KEY", "0x" + "a" * 64)
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)

    module = _load_script(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "HL_ACCOUNT_ADDRESS" in err
    assert "HL_API_KEY" not in err


def test_main_prints_report_with_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HL_API_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0x" + "b" * 40)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)

    module = _load_script(monkeypatch)

    fake_info = MagicMock()
    fake_info.user_state.return_value = {"marginSummary": {"accountValue": "1234.56"}}
    fake_info.meta.return_value = {
        "universe": [
            {"name": "BTC"},
            {"name": "ETH"},
            {"name": "SOL"},
            {"name": "ARB"},
            {"name": "DOGE"},
            {"name": "AVAX"},
        ]
    }
    fake_info.all_mids.return_value = {"BTC": "65432.1", "ETH": "3210.0"}
    module._fake_info_cls.return_value = fake_info

    rc = module.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "Hyperliquid testnet connection OK" in out
    assert "1234.56" in out
    assert "BTC, ETH, SOL, ARB, DOGE" in out
    assert "65432.1" in out
    # Ensure testnet URL was used
    module._fake_info_cls.assert_called_once()
    args, _kwargs = module._fake_info_cls.call_args
    assert args[0] == "https://api.hyperliquid-testnet.xyz"


def test_next_funding_time_rolls_to_top_of_next_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script(monkeypatch)

    now = datetime(2026, 4, 28, 5, 17, 30, tzinfo=timezone.utc)
    nxt = module.next_funding_time(now)
    assert nxt == datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)

    on_the_hour = datetime(2026, 4, 28, 5, 0, 0, tzinfo=timezone.utc)
    nxt2 = module.next_funding_time(on_the_hour)
    assert nxt2 == datetime(2026, 4, 28, 6, 0, 0, tzinfo=timezone.utc)
