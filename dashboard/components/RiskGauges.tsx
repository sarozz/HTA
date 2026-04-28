"use client";

import type { Position, RiskEvent, Stats } from "@/lib/types";
import { cn } from "@/lib/cn";
import { clockTime, money, pct } from "@/lib/format";

interface RiskGaugesProps {
  stats: Stats | null;
  positions: Position[];
  riskEvents: RiskEvent[];
  equity: number;
  loading: boolean;
}

export function RiskGauges({
  stats,
  positions,
  riskEvents,
  equity,
  loading,
}: RiskGaugesProps) {
  // Daily / weekly limits per RISK.md (2% / 5% of equity).
  const dailyPnl = stats?.net_pnl ?? 0;
  const dailyUsed = Math.max(0, -dailyPnl);
  const dailyLimit = equity * 0.02;
  const dailyRatio = dailyLimit > 0 ? dailyUsed / dailyLimit : 0;

  // Weekly is a placeholder until /api/stats?window=7d is wired.
  const weeklyUsed = dailyUsed; // approximation for the demo
  const weeklyLimit = equity * 0.05;
  const weeklyRatio = weeklyLimit > 0 ? weeklyUsed / weeklyLimit : 0;

  // Account leverage = sum |notional| / equity.
  const grossNotional = positions.reduce((acc, p) => {
    const sz = parseFloat(p.size);
    const mark = parseFloat(p.mark_price);
    return acc + Math.abs(sz * mark);
  }, 0);
  const leverage = equity > 0 ? grossNotional / equity : 0;
  const leverageRatio = leverage / 3.0; // cap at 3x

  return (
    <section className="card p-4 h-full flex flex-col gap-3">
      <header>
        <span className="label">Risk</span>
      </header>

      <Bar
        label="Daily loss"
        used={dailyUsed}
        limit={dailyLimit}
        ratio={dailyRatio}
        loading={loading}
      />
      <Bar
        label="Weekly loss"
        used={weeklyUsed}
        limit={weeklyLimit}
        ratio={weeklyRatio}
        loading={loading}
      />
      <LeverageBar value={leverage} loading={loading} ratio={leverageRatio} />

      <LiqDistance positions={positions} loading={loading} />

      <div className="border-t border-border pt-2 mt-1 flex-1 min-h-0 overflow-auto">
        <div className="label mb-1">Risk feed</div>
        {loading ? (
          <div className="skeleton h-16 w-full" />
        ) : riskEvents.length === 0 ? (
          <div className="text-text2 text-sm">no events yet</div>
        ) : (
          <ul className="space-y-1">
            {riskEvents.slice(0, 10).map((e, i) => (
              <li
                key={`${e.ts}-${i}`}
                className="flex items-baseline gap-2 text-xs"
              >
                <span className="num text-text2 w-20 shrink-0">
                  {clockTime(e.ts).slice(11, 19)}
                </span>
                <span
                  className={cn(
                    "uppercase tracking-wider w-24 shrink-0 truncate",
                    eventColor(e.kind),
                  )}
                >
                  {e.kind.replaceAll("_", " ")}
                </span>
                <span className="text-text1 truncate">
                  {e.symbol ? `${e.symbol} · ` : ""}
                  {String(e.reason ?? "")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function eventColor(kind: string): string {
  if (kind === "halt") return "text-halt";
  if (kind === "order_blocked" || kind === "order_rejected_by_risk")
    return "text-warn";
  if (kind === "order_abandoned") return "text-warn";
  return "text-text1";
}

function Bar({
  label,
  used,
  limit,
  ratio,
  loading,
}: {
  label: string;
  used: number;
  limit: number;
  ratio: number;
  loading: boolean;
}) {
  const clamped = Math.max(0, Math.min(ratio, 1));
  const danger = ratio > 0.8;
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs mb-1">
        <span className="text-text1">{label}</span>
        <span className="num text-text2">
          {loading ? "…" : `${money(used)} / ${money(limit)}`}
        </span>
      </div>
      <div className="h-1.5 bg-bg2 rounded overflow-hidden">
        <div
          className={cn(
            "h-full transition-all duration-200",
            danger ? "bg-bear" : "bg-bull",
          )}
          style={{ width: `${clamped * 100}%` }}
        />
      </div>
    </div>
  );
}

function LeverageBar({
  value,
  loading,
  ratio,
}: {
  value: number;
  loading: boolean;
  ratio: number;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs mb-1">
        <span className="text-text1">Leverage</span>
        <span className="num text-text2">
          {loading ? "…" : `${value.toFixed(2)}x / 3.00x`}
        </span>
      </div>
      <div className="h-1.5 bg-bg2 rounded overflow-hidden">
        <div
          className={cn(
            "h-full transition-all duration-200",
            ratio > 0.85 ? "bg-warn" : "bg-accent",
          )}
          style={{ width: `${Math.min(ratio, 1) * 100}%` }}
        />
      </div>
    </div>
  );
}

function LiqDistance({
  positions,
  loading,
}: {
  positions: Position[];
  loading: boolean;
}) {
  if (loading) return null;
  const open = positions.filter((p) => parseFloat(p.size) !== 0);
  if (open.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className="label">Liq distance</div>
      {open.map((p) => {
        const mark = parseFloat(p.mark_price);
        const liq = parseFloat(p.liq_price ?? "0");
        const dist = liq > 0 && mark > 0 ? Math.abs(mark - liq) / mark : null;
        const ratio = dist === null ? 0 : Math.max(0, Math.min(dist / 0.5, 1));
        const tight = dist !== null && dist < 0.08;
        return (
          <div key={p.symbol} className="flex items-center gap-2 text-xs">
            <span className="num w-12 text-text1">{p.symbol}</span>
            <div className="flex-1 h-1.5 bg-bg2 rounded overflow-hidden">
              <div
                className={cn(
                  "h-full transition-all duration-200",
                  tight ? "bg-bear" : "bg-accent",
                )}
                style={{ width: `${ratio * 100}%` }}
              />
            </div>
            <span className="num text-text2 w-10 text-right">
              {dist === null ? "—" : pct(dist)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
