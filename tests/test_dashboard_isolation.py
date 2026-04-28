"""Hard isolation contract for dashboard_api.

The dashboard_api module must not pull the Hyperliquid SDK, the
exchange adapter, the order router, the kill switch, the position
tracker, or src.main into sys.modules. Those modules either hold
keys (SDK, adapter, exchange) or have side-effecting trade-side
state (router, kill switch). Keeping them out is the only way the
"read-only" contract is enforceable.

This test runs in a fresh subprocess so it can't be polluted by other
tests that have already loaded the bot.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_MODULES = (
    "hyperliquid",
    "hyperliquid.exchange",
    "hyperliquid.info",
    "src.execution.exchange_adapter",
    "src.execution.order_router",
    "src.execution.position_tracker",
    "src.risk.kill_switch",
    "src.main",
    "eth_account",
)


def _run_isolated(snippet: str) -> tuple[int, str, str]:
    completed = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def test_importing_dashboard_api_does_not_load_sdk_or_keys() -> None:
    """The big one: import the whole dashboard_api package and verify
    none of the forbidden modules are in sys.modules afterwards."""
    forbidden_repr = repr(list(FORBIDDEN_MODULES))
    snippet = textwrap.dedent(f"""
        import sys
        # Importing the whole package transitively pulls in main, routes, ws, etc.
        import src.dashboard_api  # noqa: F401
        import src.dashboard_api.main  # noqa: F401
        import src.dashboard_api.routes  # noqa: F401
        import src.dashboard_api.ws  # noqa: F401
        import src.dashboard_api.queries  # noqa: F401
        import src.dashboard_api.auth  # noqa: F401
        import src.dashboard_api.bus  # noqa: F401

        forbidden = {forbidden_repr}
        leaked = [m for m in forbidden if m in sys.modules]
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        print("OK")
        """)
    rc, stdout, stderr = _run_isolated(snippet)
    assert rc == 0, f"isolation check failed:\nstdout={stdout}\nstderr={stderr}"
    assert "OK" in stdout


def test_dashboard_api_bus_imports_only_stdlib() -> None:
    """bus.py is the most security-sensitive module — the trading loop
    imports it. Keep it on stdlib only."""
    snippet = textwrap.dedent("""
        import sys
        before = set(sys.modules)
        import src.dashboard_api.bus  # noqa: F401
        after = set(sys.modules)
        added = after - before
        # Filter to top-level package names only.
        toplevel = {m.split('.')[0] for m in added if '.' not in m or m.startswith('src.')}
        # Allowed: stdlib modules + src.dashboard_api.bus itself.
        allowed_first_party = {'src'}
        # Find any third-party packages.
        import importlib.util
        third_party = set()
        for m in toplevel - allowed_first_party:
            spec = importlib.util.find_spec(m)
            if spec is None or spec.origin is None:
                continue
            origin = spec.origin
            if 'site-packages' in origin or 'dist-packages' in origin:
                third_party.add(m)
        if third_party:
            print('THIRDPARTY:' + ','.join(sorted(third_party)))
            sys.exit(1)
        print('OK')
        """)
    rc, stdout, stderr = _run_isolated(snippet)
    assert rc == 0, f"bus.py pulled in third-party deps:\nstdout={stdout}\nstderr={stderr}"


def test_dashboard_api_assertion_helper_works() -> None:
    """If a future change introduces a forbidden import, the runtime
    helper in main.py raises ImportError."""
    snippet = textwrap.dedent("""
        import os
        os.environ['HTA_ASSERT_DASHBOARD_ISOLATION'] = '1'
        import sys
        # Force a forbidden module into sys.modules BEFORE importing main —
        # then the assertion helper should fire.
        import unittest.mock as _mock
        sys.modules['hyperliquid'] = _mock.MagicMock()
        try:
            import src.dashboard_api.main  # noqa: F401
        except ImportError as exc:
            assert 'forbidden modules' in str(exc), exc
            print('OK')
        else:
            print('FAIL: assertion did not fire')
            sys.exit(1)
        """)
    rc, stdout, stderr = _run_isolated(snippet)
    assert rc == 0, f"assertion helper broken:\nstdout={stdout}\nstderr={stderr}"
    assert "OK" in stdout


def test_routes_do_not_import_order_router() -> None:
    """Direct import of routes.py must not pull the order router."""
    snippet = textwrap.dedent("""
        import sys
        import src.dashboard_api.routes  # noqa: F401
        for m in ('src.execution.order_router', 'src.execution.exchange_adapter'):
            assert m not in sys.modules, f'leaked {m}'
        print('OK')
        """)
    rc, stdout, stderr = _run_isolated(snippet)
    assert rc == 0, f"routes.py leaks bot internals:\nstdout={stdout}\nstderr={stderr}"
