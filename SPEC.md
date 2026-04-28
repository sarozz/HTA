# Strategy Specification

## Strategy A — Intraday Mean Reversion

Universe: BTC-PERP, ETH-PERP, SOL-PERP on Hyperliquid.
Timeframe: 5-minute candles.

Features (computed at each bar close, lagged by one bar in the simulator):
- z_score = (close - SMA(close, 96)) / STDEV(close, 96)
- rsi = RSI(close, 14)

Entry rules:
- LONG when z_score < -2.0 AND rsi < 30
- SHORT when z_score > +2.0 AND rsi > 70
- All entries via post-only (ALO) limit orders at the touch.
- If ALO rejected, retry once with one tick of price improvement,
  then abandon the trade.

Exit rules (whichever fires first):
- Target: |z_score| crosses 0
- Stop:   |z_score| > 3.5
- Time stop: position held > 4 bars

Position sizing:
- Risk per trade = 0.5% of account equity
- Position notional = risk_dollars / stop_distance_pct
- Cap notional at 30% of equity regardless

Expected stats (verify in backtest):
- Win rate 52-58%, avg win/loss ~1.0
- 4-10 trades/day across 3 symbols
- Net edge after fees: 2-6 bps per trade

## Strategy B — Funding Rate Capture

Universe: BTC, ETH, SOL (must have both spot and perp on Hyperliquid).

Trigger: predicted next 1-hour funding > 0.10%.

Execution:
1. 10 minutes before the funding tick, place two ALO orders:
   - SHORT perp at the bid
   - LONG  spot at the ask
   Same notional. Use up to 50% of free margin per leg.
2. If both fill within 90 seconds, hold through the funding tick.
3. If one fills and the other doesn't within 90 seconds, cancel both
   and unwind any filled leg with a market order. Never run unhedged.
4. 1-2 minutes after the tick (funding paid), close both legs with
   ALO orders at the touch.

Profitability gate: predicted funding must exceed 0.10% to clear
the round-trip cost (4 x 0.015% maker = 6 bps) plus a safety margin.

Expected stats:
- 5-20 captures per week typically
- Win rate 85-95% (failure mode is execution, not thesis)
- Net per capture: 2-8 bps
