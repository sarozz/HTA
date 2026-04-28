"""Live event loop for Strategy A on Hyperliquid testnet.

Wires together:
  - HyperliquidWsClient        — l2Book + trades subscriptions
  - CandleStore                — builds 5m OHLCV from the trade stream
  - PositionTracker            — caches positions/orders from /info
  - HyperliquidExchangeAdapter — async wrapper over Info + Exchange
  - KillSwitch                 — halt sequence + HALTED flag
  - OrderRouter                — translates targets to ALO orders
  - Reconciler                 — 30s divergence check
  - mean_reversion.evaluate    — pure strategy

Tick model:
  - The trades stream feeds CandleStore continuously.
  - Once per second, we check whether any symbol has a NEW closed bar
    (open_time != open_time of the previous tick). For each newly
    closed bar we evaluate Strategy A and submit the resulting target.
  - In parallel, the reconciler runs every 30s and the router
    cancels stale orders every 5s.

Per CLAUDE.md non-negotiable #1: this script is hardcoded to TESTNET.
There is no mainnet flag. Move to mainnet by changing config + URLs
in a separate, deliberate change set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.dashboard_api.bus import (
    CHANNEL_EQUITY,
    CHANNEL_HEARTBEAT,
    CHANNEL_POSITION,
    CHANNEL_RISK_EVENT,
    CHANNEL_SIGNAL,
    Bus,
)
from src.execution.capital_allocator import (
    CapitalAllocatorConfig,
    allocate,
)
from src.execution.capital_allocator import (
    default_config as default_allocator_config,
)
from src.execution.exchange_adapter import HyperliquidExchangeAdapter
from src.execution.funding_rate_oracle import FundingRateOracle
from src.execution.order_router import OrderRouter
from src.execution.position_tracker import PositionTracker
from src.execution.reconciler import Reconciler
from src.marketdata.candle_store import CandleStore
from src.marketdata.ws_client import (
    CHANNEL_DISCONNECTED,
    CHANNEL_STALE,
    CHANNEL_TRADES,
    HyperliquidWsClient,
)
from src.notify.telegram import TelegramNotifier
from src.risk.kill_switch import KillSwitch
from src.storage.journal import Journal, JournalLike
from src.strategies.funding_capture import (
    AccountSnapshot,
    FundingCaptureConfig,
    FundingCaptureDecision,
    FundingCaptureSymbolState,
)
from src.strategies.funding_capture import (
    evaluate as evaluate_funding,
)
from src.strategies.mean_reversion import (
    MeanReversionConfig,
    StrategyPosition,
    TargetPosition,
    evaluate,
)

logger = logging.getLogger(__name__)

TESTNET_REST_URL = "https://api.hyperliquid-testnet.xyz"
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"


@dataclass
class _SymbolState:
    last_closed_bar_open_ms: int | None = None
    entry_open_ms: int | None = None
    entry_price: Decimal = Decimal("0")
    bars_held: int = 0


class SimpleTickService:
    """Fallback TickService driven from the latest trade prices.

    Tick size is 1e-2 for now (correct for BTC/ETH/SOL on Hyperliquid;
    a meta-aware service replaces this once we wire info.meta).
    """

    def __init__(self, candle_store: CandleStore, default_tick: Decimal = Decimal("0.01")) -> None:
        self._store = candle_store
        self._default_tick = default_tick

    def tick_size(self, symbol: str) -> Decimal:
        return self._default_tick

    def best_bid(self, symbol: str) -> Decimal | None:
        df = self._store.get_candles(symbol)
        if df.empty:
            return None
        last_close = Decimal(str(df.iloc[-1].close))
        return last_close - self._default_tick

    def best_ask(self, symbol: str) -> Decimal | None:
        df = self._store.get_candles(symbol)
        if df.empty:
            return None
        last_close = Decimal(str(df.iloc[-1].close))
        return last_close + self._default_tick


@dataclass
class LiveConfig:
    symbols: list[str]
    history: int
    bar_seconds: int
    sma_window: int
    rsi_window: int
    z_entry: Decimal
    z_target: Decimal
    z_stop: Decimal
    rsi_oversold: Decimal
    rsi_overbought: Decimal
    time_stop_bars: int
    risk_per_trade_pct: Decimal
    max_notional_pct: Decimal
    reconciler_interval_seconds: float
    stale_order_seconds: float
    journal_path: Path

    @staticmethod
    def from_yaml(path: Path) -> LiveConfig:
        with path.open() as f:
            cfg = yaml.safe_load(f) or {}
        mr = cfg.get("mean_reversion", {})
        risk = cfg.get("risk", {})
        rec = cfg.get("reconciler", {})
        storage = cfg.get("storage", {})
        return LiveConfig(
            symbols=[s.replace("-PERP", "") for s in mr.get("universe", ["BTC", "ETH", "SOL"])],
            history=int(mr.get("sma_window", 96)) + 50,
            bar_seconds=300,
            sma_window=int(mr.get("sma_window", 96)),
            rsi_window=int(mr.get("rsi_window", 14)),
            z_entry=Decimal(str(mr.get("z_entry_short", 2.0))),
            z_target=Decimal(str(mr.get("z_target", 0.0))),
            z_stop=Decimal(str(mr.get("z_stop", 3.5))),
            rsi_oversold=Decimal(str(mr.get("rsi_oversold", 30))),
            rsi_overbought=Decimal(str(mr.get("rsi_overbought", 70))),
            time_stop_bars=int(mr.get("time_stop_bars", 4)),
            risk_per_trade_pct=Decimal(str(risk.get("max_risk_per_trade_pct", 0.005))),
            max_notional_pct=Decimal(str(risk.get("max_position_notional_pct", 0.30))),
            reconciler_interval_seconds=float(rec.get("interval_seconds", 30.0)),
            stale_order_seconds=60.0,
            journal_path=Path(storage.get("sqlite_path", "data/journal.sqlite")),
        )

    def to_strategy_config(self) -> MeanReversionConfig:
        return MeanReversionConfig(
            sma_window=self.sma_window,
            rsi_window=self.rsi_window,
            z_entry=self.z_entry,
            z_target=self.z_target,
            z_stop=self.z_stop,
            rsi_oversold=self.rsi_oversold,
            rsi_overbought=self.rsi_overbought,
            time_stop_bars=self.time_stop_bars,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_notional_pct=self.max_notional_pct,
        )


class LiveSystem:
    """Holds all the wired components and runs the loop.

    Two strategies coexist:
      - mean_reversion (Strategy A): bar-close evaluation on 5m candles
      - funding_capture (Strategy B): time-based scheduler around hourly
        funding ticks; PERP-ONLY ROUTING IS DISABLED until a spot
        adapter exists (we never send unhedged orders per SPEC).

    Capital is split via CapitalAllocator: A=25%, B=50%, buffer=25%.
    Per-strategy used notional is tracked locally because the exchange
    only knows the net per-coin position.
    """

    def __init__(
        self,
        config: LiveConfig,
        adapter: HyperliquidExchangeAdapter,
        ws_client: HyperliquidWsClient,
        candle_store: CandleStore,
        tracker: PositionTracker,
        kill_switch: KillSwitch,
        router: OrderRouter,
        reconciler: Reconciler,
        journal: JournalLike,
        funding_oracle: FundingRateOracle | None = None,
        capital_config: CapitalAllocatorConfig | None = None,
        funding_config: FundingCaptureConfig | None = None,
        spot_adapter: object | None = None,
        bus: Bus | None = None,
    ) -> None:
        self._config = config
        self._bus = bus or Bus()
        self._adapter = adapter
        self._ws = ws_client
        self._candles = candle_store
        self._tracker = tracker
        self._kill = kill_switch
        self._router = router
        self._reconciler = reconciler
        self._journal = journal
        self._symbol_state: dict[str, _SymbolState] = {s: _SymbolState() for s in config.symbols}
        self._stop_event = asyncio.Event()

        # Strategy A's used notional, indexed by coin. Updated whenever A
        # opens or closes a position. Used by the capital allocator.
        self._a_used_notional: Decimal = Decimal("0")

        # Strategy B state
        self._funding_oracle = funding_oracle
        self._capital_config = capital_config or default_allocator_config()
        self._funding_config = funding_config or FundingCaptureConfig()
        self._funding_state: dict[str, FundingCaptureSymbolState] = {
            coin: FundingCaptureSymbolState(coin=coin) for coin in config.symbols
        }
        self._spot_adapter = spot_adapter  # None until spot routing is wired

    def stop(self) -> None:
        self._stop_event.set()
        self._ws.stop()
        self._reconciler.stop()

    @property
    def bus(self) -> Bus:
        return self._bus

    async def run(self) -> int:
        await self._journal.append(
            "live_start",
            {"network": "testnet", "symbols": self._config.symbols, "url": TESTNET_REST_URL},
        )
        await self._tracker.refresh()

        ws_task = asyncio.create_task(self._ws.run(), name="ws")
        rec_task = asyncio.create_task(self._reconciler.run(), name="reconciler")
        bar_task = asyncio.create_task(self._bar_close_loop(), name="bar-close")
        cancel_task = asyncio.create_task(self._stale_cancel_loop(), name="stale-cancel")
        funding_task = asyncio.create_task(self._funding_capture_loop(), name="funding")
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        oracle_task = (
            asyncio.create_task(self._funding_oracle.run(), name="oracle")
            if self._funding_oracle is not None
            else None
        )
        all_tasks = [
            t
            for t in (
                ws_task,
                rec_task,
                bar_task,
                cancel_task,
                funding_task,
                heartbeat_task,
                oracle_task,
            )
            if t is not None
        ]

        try:
            await self._stop_event.wait()
        finally:
            for t in all_tasks:
                t.cancel()
            for t in all_tasks:
                with suppress(asyncio.CancelledError):
                    await t
            await self._journal.append("live_stop", {})
        return 0

    async def _bar_close_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                pass
            for symbol in self._config.symbols:
                try:
                    await self._maybe_evaluate_symbol(symbol)
                except Exception:
                    logger.exception("evaluate failed for %s", symbol)

    async def _stale_cancel_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._router.cancel_stale_orders()
            except Exception:
                logger.exception("stale cancel pass failed")

    async def _heartbeat_loop(self) -> None:
        """Publishes equity + heartbeat ticks to the bus and journals
        equity_tick rows so the dashboard API can render time series."""
        import time as _time

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass
            try:
                equity = self._tracker.equity
                positions = self._tracker.positions()
                unrealized = sum(
                    (p.size * (p.mark_price - p.entry_price) for p in positions),
                    Decimal("0"),
                )
                payload = {
                    "ts": _time.time(),
                    "equity": str(equity),
                    "realized": str(equity - unrealized),
                    "unrealized": str(unrealized),
                }
                self._bus.publish(CHANNEL_EQUITY, payload)
                self._bus.publish(
                    CHANNEL_HEARTBEAT,
                    {"halted": self._kill.halted, "subs": self._bus.subscriber_count},
                )
                # Persist the equity tick + heartbeat marker so post-hoc
                # /api/equity and /api/health work.
                await self._journal.append("equity_tick", payload)
                await self._journal.upsert_state(
                    "heartbeat",
                    "_",
                    {"ts": _time.time(), "halted": self._kill.halted},
                )
                # Update per-symbol position state.
                for p in positions:
                    pos_payload = {
                        "symbol": p.symbol,
                        "size": str(p.size),
                        "entry_price": str(p.entry_price),
                        "mark_price": str(p.mark_price),
                        "liq_price": str(p.liq_price) if p.liq_price is not None else None,
                    }
                    self._bus.publish(CHANNEL_POSITION, pos_payload)
                    await self._journal.upsert_state("position", p.symbol, pos_payload)
            except Exception:
                logger.exception("heartbeat tick failed")

    async def _funding_capture_loop(self) -> None:
        """Strategy B scheduler tick. Runs every 5 seconds.

        At each tick:
          1. Pull the latest funding rates from the oracle (read-only cache).
          2. Compute capital allocation given A and B's current usage.
          3. Run the funding-capture state machine for each coin.
          4. Dispatch decisions; suppress order placement if the spot
             adapter is not wired (avoid unhedged perp exposure).
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                return
            except asyncio.TimeoutError:
                pass
            if self._kill.halted:
                continue
            try:
                await self._funding_capture_tick()
            except Exception:
                logger.exception("funding capture tick failed")

    async def _funding_capture_tick(self) -> None:
        rates = self._funding_oracle.latest if self._funding_oracle else {}
        if not rates and not any(s.phase != "idle" for s in self._funding_state.values()):
            return  # nothing to do
        equity = self._tracker.equity if self._tracker.equity > 0 else Decimal("1500")
        b_used = sum(
            (abs(s.target_notional) for s in self._funding_state.values()),
            Decimal("0"),
        )
        allocations = allocate(
            equity=equity,
            used_by_strategy={
                "mean_reversion": self._a_used_notional,
                "funding_capture": b_used,
            },
            config=self._capital_config,
        )
        b_alloc = allocations.get("funding_capture")
        if b_alloc is None:
            return

        account = AccountSnapshot(
            equity=equity,
            available_notional_for_strategy=b_alloc.available_notional,
        )
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        now = _dt.now(_tz.utc)
        decisions = evaluate_funding(
            now=now,
            funding_rates=rates,
            account=account,
            states=self._funding_state,
            config=self._funding_config,
        )
        for decision in decisions:
            self._funding_state[decision.coin] = decision.next_state
            await self._dispatch_funding_decision(decision, b_alloc.degraded)

    async def _dispatch_funding_decision(
        self, decision: FundingCaptureDecision, degraded: bool
    ) -> None:
        if decision.perp_target is None and decision.spot_target is None:
            return
        # Always journal the decision so we can reconstruct what the
        # scheduler was thinking even when we suppress order placement.
        await self._journal.append(
            "funding_capture_decision",
            {
                "coin": decision.coin,
                "phase": decision.next_state.phase,
                "perp_target_notional": (
                    str(decision.perp_target.notional) if decision.perp_target else None
                ),
                "spot_target_notional": (
                    str(decision.spot_target.notional) if decision.spot_target else None
                ),
                "reason": decision.reason,
                "capital_degraded": degraded,
            },
        )
        if self._spot_adapter is None:
            # Safety: no spot route → never send the perp leg either.
            # Strategy B runs in observation-only mode until spot wires.
            await self._journal.append(
                "funding_capture_skipped",
                {
                    "coin": decision.coin,
                    "reason": (
                        "spot adapter not wired; suppressing perp leg to " "avoid unhedged exposure"
                    ),
                },
            )
            return
        # Spot is wired (future): submit both legs.
        if decision.perp_target is not None:
            await self._router.submit_target(decision.perp_target, strategy="funding_capture")
        # NOTE: spot routing goes here once the spot router lands.

    async def _maybe_evaluate_symbol(self, symbol: str) -> None:
        if self._kill.halted:
            return
        df = self._candles.get_candles(symbol)
        if df.empty:
            return

        # Detect a newly closed bar: the latest bar in the dataframe is
        # the in-progress bar; the previous one is the most recently
        # closed. We evaluate when we first observe a new "previous"
        # entry compared to the last tick.
        if len(df) < 2:
            return
        latest_closed_open_ms = int(df.index[-2].value // 1_000_000)

        state = self._symbol_state[symbol]
        if state.last_closed_bar_open_ms == latest_closed_open_ms:
            return  # already handled this bar
        state.last_closed_bar_open_ms = latest_closed_open_ms

        # Construct a StrategyPosition for the strategy. bars_held is
        # incremented at every observed bar close while we hold.
        tracked = self._tracker.get_position(symbol)
        sp: StrategyPosition | None = None
        if tracked is not None and tracked.size != 0:
            if state.entry_open_ms is None:
                state.entry_open_ms = latest_closed_open_ms
                state.entry_price = tracked.entry_price
                state.bars_held = 0
            else:
                state.bars_held += 1
            sp = StrategyPosition(
                symbol=symbol,
                size=tracked.size,
                entry_price=state.entry_price or tracked.entry_price,
                bars_held=state.bars_held,
            )
        else:
            state.entry_open_ms = None
            state.bars_held = 0

        # Evaluate up through the just-closed bar (drop the in-progress one).
        candles_for_eval = df.iloc[:-1]
        target: TargetPosition = evaluate(
            symbol=symbol,
            candles=candles_for_eval,
            position=sp,
            equity=self._tracker.equity if self._tracker.equity > 0 else Decimal("1500"),
            config=self._config.to_strategy_config(),
        )

        signal_payload = {
            "symbol": symbol,
            "strategy": "mean_reversion",
            "bar_open_ms": latest_closed_open_ms,
            "target_notional": str(target.notional),
            "reason": target.reason,
            "current_size": str(tracked.size) if tracked else "0",
        }
        await self._journal.append("signal", signal_payload)
        self._bus.publish(CHANNEL_SIGNAL, signal_payload)

        # Refresh tracker before submission so the delta computation uses
        # the freshest possible truth.
        try:
            await self._tracker.refresh()
        except Exception:
            logger.exception("tracker refresh before submission failed")
            return

        # Apply Strategy A's capital cap. The router still calls
        # check_order, but we additionally throttle entries here so the
        # allocator's degrade contract is honoured before the order even
        # reaches the risk manager.
        equity = self._tracker.equity if self._tracker.equity > 0 else Decimal("1500")
        b_used = sum(
            (abs(s.target_notional) for s in self._funding_state.values()),
            Decimal("0"),
        )
        allocations = allocate(
            equity=equity,
            used_by_strategy={
                "mean_reversion": self._a_used_notional,
                "funding_capture": b_used,
            },
            config=self._capital_config,
        )
        a_alloc = allocations.get("mean_reversion")
        if (
            a_alloc is not None
            and abs(target.notional) > a_alloc.available_notional + a_alloc.used_notional
        ):
            risk_payload = {
                "kind": "signal_suppressed",
                "symbol": symbol,
                "reason": "exceeds A's capital allocation",
                "wanted": str(target.notional),
                "available": str(a_alloc.available_notional),
            }
            await self._journal.append("signal_suppressed", risk_payload)
            self._bus.publish(CHANNEL_RISK_EVENT, risk_payload)
            return

        await self._router.submit_target(target, strategy="mean_reversion")
        # Track A's used notional locally (the exchange returns net per coin
        # which we can't disambiguate from B once both are live).
        self._a_used_notional = abs(target.notional)

    # --- handlers wired by the factory ----------------------------------

    def _handle_trades(self, data: Any) -> None:
        if isinstance(data, list):
            self._candles.on_trades_message(data)

    async def _handle_stale(self, data: Any) -> None:
        await self._kill.halt(f"ws stale: {data}")

    async def _handle_disconnected(self, data: Any) -> None:
        await self._journal.append("ws_disconnected", {"data": data})


async def build_system(config_path: Path) -> LiveSystem:
    """Construct LiveSystem from environment + yaml config. Testnet only."""
    load_dotenv()
    api_key = os.environ.get("HL_API_KEY")
    address = os.environ.get("HL_ACCOUNT_ADDRESS")
    if not api_key or not address:
        raise RuntimeError(
            "HL_API_KEY and HL_ACCOUNT_ADDRESS must be set in the environment "
            "(see .env.example)."
        )

    # Lazy SDK import so the rest of the system is testable without it.
    from eth_account import Account  # type: ignore[import-untyped]
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    cfg = LiveConfig.from_yaml(config_path)
    info = Info(TESTNET_REST_URL, skip_ws=True)
    wallet = Account.from_key(api_key)
    exchange = Exchange(wallet, TESTNET_REST_URL, account_address=address)
    adapter = HyperliquidExchangeAdapter(info=info, exchange=exchange, address=address)

    journal = Journal(cfg.journal_path)
    notifier = TelegramNotifier(journal)
    kill_switch = KillSwitch(adapter, journal, notifier)
    candle_store = CandleStore(history=cfg.history, bar_seconds=cfg.bar_seconds)
    tracker = PositionTracker(adapter)
    tick_service = SimpleTickService(candle_store)
    bus = Bus()
    router = OrderRouter(
        adapter=adapter,
        tracker=tracker,
        kill_switch=kill_switch,
        tick_service=tick_service,
        journal=journal,
        stale_seconds=cfg.stale_order_seconds,
        bus=bus,
    )
    reconciler = Reconciler(
        tracker=tracker,
        kill_switch=kill_switch,
        journal=journal,
        interval_seconds=cfg.reconciler_interval_seconds,
    )

    ws_client = HyperliquidWsClient(TESTNET_WS_URL, cfg.symbols)
    funding_oracle = FundingRateOracle(info=info, coins=cfg.symbols)
    system = LiveSystem(
        config=cfg,
        adapter=adapter,
        ws_client=ws_client,
        candle_store=candle_store,
        tracker=tracker,
        kill_switch=kill_switch,
        router=router,
        reconciler=reconciler,
        journal=journal,
        funding_oracle=funding_oracle,
        # spot_adapter intentionally None — Strategy B runs in
        # observation-only mode until a spot router is wired.
        spot_adapter=None,
        bus=bus,
    )

    ws_client.on(CHANNEL_TRADES, system._handle_trades)
    ws_client.on(CHANNEL_STALE, system._handle_stale)
    ws_client.on(CHANNEL_DISCONNECTED, system._handle_disconnected)

    return system


async def main(config_path: Path | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg_path = config_path or Path("config/example.yaml")
    system = await build_system(cfg_path)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, system.stop)

    return await system.run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
