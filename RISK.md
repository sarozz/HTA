# Risk Management Contract

These are HARD CAPS. The Risk Manager enforces them. Every order
intent must pass check_order(intent, state) before reaching the
Order Router.

## Per-trade caps
- Max risk per trade:      0.5% of equity
- Max position notional:   30% of equity
- Max leverage (account):  3.0x
- Liquidation buffer:      >= 8% from current mark

## Concurrency caps
- Max concurrent positions:        2 (one per strategy)
- Max open orders:                 6
- Max orders submitted per minute: 20

## Loss limits (auto-halt when hit)
- Daily loss limit:    2% of equity (resets at 00:00 UTC)
- Weekly loss limit:   5% of equity (requires manual resume)
- Consecutive losers:  3 -> 1-hour pause

## Kill switch contract
The function halt(reason: str) MUST:
1. Set a global HALTED flag.
2. Cancel all open orders on the exchange.
3. Close all open positions with reduce-only market orders.
4. Write the halt reason and timestamp to the journal.
5. Notify via Telegram.

The HALTED flag is checked before every order intent. While HALTED,
only manual /resume from Telegram clears it.

## Reconciliation contract
Every 30 seconds, the Reconciler MUST:
1. Query the exchange for actual positions and open orders.
2. Compare to local state.
3. On any divergence, call halt("reconciliation divergence: ...").

## Failure modes that trigger halt
- WebSocket silent for > 30 seconds
- Three consecutive order rejections from the exchange
- Account equity drops below 90% of session-start equity
- Any unhandled exception in the main loop
- Reconciliation divergence
