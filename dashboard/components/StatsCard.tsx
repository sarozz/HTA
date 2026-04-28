"use client";

import { useState } from "react";
import useSWR from "swr";

import type { Stats } from "@/lib/types";
import { fetchStats } from "@/lib/api";
import { cn } from "@/lib/cn";
import { money, pct } from "@/lib/format";

const WINDOWS = ["1h", "24h", "7d", "30d", "all"] as const;
type Window = (typeof WINDOWS)[number];

export function StatsCard({ initial }: { initial: Stats | null }) {
  const [window, setWindow] = useState<Window>("24h");
  const { data } = useSWR<Stats>(
    `stats:${window}`,
    () => fetchStats(window),
    {
      refreshInterval: 5_000,
      fallbackData: window === "24h" ? (initial ?? undefined) : undefined,
    },
  );

  return (
    <section className="card p-4 h-full flex flex-col">
      <header className="flex items-center justify-between mb-3">
        <span className="label">Stats</span>
        <div className="flex items-center gap-1 text-xs">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className={cn(
                "px-2 py-0.5 rounded uppercase tracking-wider",
                window === w
                  ? "bg-bg2 text-text0"
                  : "text-text2 hover:text-text1",
              )}
            >
              {w}
            </button>
          ))}
        </div>
      </header>
      <dl className="grid grid-cols-2 gap-y-2 gap-x-4 text-xs flex-1">
        <Stat label="Trades" value={data?.trades ?? "—"} />
        <Stat label="Win rate" value={data ? pct(data.win_rate) : "—"} />
        <Stat
          label="Profit factor"
          value={data?.profit_factor != null ? data.profit_factor.toFixed(2) : "—"}
        />
        <Stat label="Gross P&L" value={data ? money(data.gross_pnl) : "—"} />
        <Stat label="Fees" value={data ? money(data.fees) : "—"} />
        <Stat
          label="Net P&L"
          value={data ? money(data.net_pnl, { sign: true }) : "—"}
          tone={data && data.net_pnl >= 0 ? "bull" : "bear"}
        />
        <Stat
          label="Best trade"
          value={data?.best_trade != null ? money(data.best_trade) : "—"}
          tone="bull"
        />
        <Stat
          label="Worst trade"
          value={data?.worst_trade != null ? money(data.worst_trade) : "—"}
          tone="bear"
        />
      </dl>
      <div className="border-t border-border pt-2 mt-2">
        <div className="label mb-1">Per-strategy</div>
        {data && Object.keys(data.per_strategy).length > 0 ? (
          <ul className="space-y-1 text-xs">
            {Object.entries(data.per_strategy).map(([k, v]) => (
              <li key={k} className="flex justify-between">
                <span className="text-text1">
                  {k === "mean_reversion" ? "A · mean rev" : k === "funding_capture" ? "B · funding" : k}
                </span>
                <span className="num text-text0">
                  {v.trades} · {money(v.pnl, { sign: true })}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-text2 text-xs">no closed trades yet</p>
        )}
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string | number | null;
  tone?: "default" | "bull" | "bear";
}) {
  return (
    <div className="border-b border-border/40 pb-1">
      <div className="label">{label}</div>
      <div
        className={cn(
          "num text-base",
          tone === "bull" && "text-bull",
          tone === "bear" && "text-bear",
        )}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}
