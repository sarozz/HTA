# Hyperliquid Algo Trading — Claude Code Instructions

## Mission
Build a two-strategy algo trading system for Hyperliquid (mean reversion +
funding capture) that runs on a $1,500 account with strict risk controls.

## Non-negotiables
1. ALWAYS use the testnet URL (https://api.hyperliquid-testnet.xyz)
   unless I explicitly type the word MAINNET in the prompt.
2. ALWAYS use API wallet keys, never the main wallet's private key.
3. ALWAYS check Risk Manager caps before sending an order.
4. NEVER assume local position state is correct — query the exchange.
5. NEVER add a strategy without writing a backtest for it first.
6. NEVER use placeholders like "# TODO: implement" in code that's
   meant to be runnable. If something can't be done, say so out loud.
7. NEVER place an order during a session where I haven't explicitly
   asked for one. Read-only operations are fine.

## Coding standards
- Python 3.10+, type hints everywhere, async/await for I/O.
- Pure functions for strategies; side effects only in the Order Router.
- Every order, fill, signal, and error logged as JSON to SQLite.
- pytest with at least one test per public function.
- Use ruff for linting. Use black for formatting.
- No new dependencies without asking me first.

## Architecture
See ARCHITECTURE.md. Key invariants:
- Risk Manager is the single gatekeeper for orders.
- Position Tracker is a cache; the exchange is the source of truth.
- Reconciler runs every 30 seconds.
- Kill switch can be triggered from Telegram or code; checked before
  every order.

## Project layout
```
src/
  marketdata/     # WebSocket + REST clients
  strategies/     # Pure strategy functions (mean_reversion.py, funding.py)
  risk/           # manager.py, kill_switch.py
  execution/      # order_router.py, position_tracker.py, reconciler.py
  backtest/       # vectorised backtester
  storage/        # SQLite journal
  notify/         # Telegram bot
config/
  example.yaml    # annotated; real config is .env-loaded and gitignored
scripts/          # one-off scripts (check_connection.py, etc.)
tests/            # pytest tests, mirroring src/
```

## When in doubt
Ask before: adding dependencies, touching anything that involves real
money, changing risk caps, writing a feature that isn't in SPEC.md.
