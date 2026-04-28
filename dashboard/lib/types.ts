// Mirrors the bot API payloads. Loose typing on payload bodies because
// the journal stores JSON blobs.

export type Mode = "testnet" | "mainnet" | "unknown";

export interface Heartbeat {
  ts: number | null;
  halted?: boolean;
  subs?: number;
}

export interface Health {
  heartbeat: Heartbeat;
  last_tick_ts: number | null;
  session_started_at: number | null;
  uptime_seconds: number | null;
  mode: Mode;
}

export interface EquityPoint {
  ts: number;
  equity: string | null;
  realized: string | null;
  unrealized: string | null;
}

export interface Position {
  symbol: string;
  size: string;
  entry_price: string;
  mark_price: string;
  liq_price: string | null;
  ts: number;
}

export interface Trade {
  ts: number;
  symbol: string;
  side: "buy" | "sell";
  size: string;
  price: string;
  fee?: string;
  strategy: string;
  oid?: number;
  opens_or_closes?: "open" | "close";
  pnl?: string;
}

export interface Signal {
  ts: number;
  event_type: string;
  symbol?: string;
  strategy?: string;
  reason?: string;
  target_notional?: string;
}

export interface RiskEvent {
  ts: number;
  kind: string;
  reason?: string;
  symbol?: string;
  [key: string]: unknown;
}

export interface FundingRow {
  coin: string;
  predicted_rate: string;
  next_tick: number;
  ts: number;
}

export interface Stats {
  window: string;
  trades: number;
  win_rate: number;
  profit_factor: number | null;
  gross_pnl: number;
  fees: number;
  net_pnl: number;
  best_trade: number | null;
  worst_trade: number | null;
  per_strategy: Record<string, { trades: number; pnl: number; fees: number }>;
}

export interface Snapshot {
  health: Health;
  positions: Position[];
  equity: EquityPoint[];
  signals: Signal[];
  risk_events: RiskEvent[];
  funding: FundingRow[];
  stats_24h: Stats;
}
