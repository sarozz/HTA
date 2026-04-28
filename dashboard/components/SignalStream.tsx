"use client";

import { useState } from "react";

import type { Signal } from "@/lib/types";
import { cn } from "@/lib/cn";
import { clockTime, money } from "@/lib/format";

interface SignalStreamProps {
  signals: Signal[];
  loading: boolean;
}

const FILTERS = ["all", "mean_reversion", "funding_capture"] as const;
type Filter = (typeof FILTERS)[number];

export function SignalStream({ signals, loading }: SignalStreamProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const filtered =
    filter === "all"
      ? signals
      : signals.filter((s) => s.strategy === filter);

  return (
    <section className="card p-3 h-full flex flex-col">
      <header className="flex items-center justify-between mb-2 px-1">
        <span className="label">Signals</span>
        <div className="flex items-center gap-1 text-xs">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={cn(
                "px-2 py-0.5 rounded uppercase tracking-wider",
                filter === f
                  ? "bg-bg2 text-text0"
                  : "text-text2 hover:text-text1",
              )}
            >
              {f === "all" ? "all" : f === "mean_reversion" ? "A" : "B"}
            </button>
          ))}
        </div>
      </header>
      <div className="flex-1 min-h-0 overflow-auto">
        {loading ? (
          <div className="skeleton h-32 w-full" />
        ) : filtered.length === 0 ? (
          <div className="text-text2 text-sm text-center py-8">
            no signals yet
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-text2 sticky top-0 bg-bg1">
              <tr>
                <Th className="w-24 text-left">Time</Th>
                <Th className="w-14 text-left">Sym</Th>
                <Th className="w-8 text-left">Strat</Th>
                <Th className="w-20 text-right">Notional</Th>
                <Th className="text-left">Reason</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 200).map((s, i) => (
                <tr
                  key={`${s.ts}-${i}`}
                  className="border-t border-border/50 hover:bg-bg2/50"
                >
                  <Td className="num text-text2">{clockTime(s.ts).slice(11, 19)}</Td>
                  <Td>{s.symbol ?? "—"}</Td>
                  <Td className="text-text1">
                    {s.strategy === "mean_reversion"
                      ? "A"
                      : s.strategy === "funding_capture"
                        ? "B"
                        : "?"}
                  </Td>
                  <Td className="num text-right">
                    {s.target_notional ? money(s.target_notional) : "—"}
                  </Td>
                  <Td className="text-text1 truncate">{s.reason ?? "—"}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <th
      className={cn(
        "uppercase tracking-wider font-medium px-1.5 py-1.5",
        className,
      )}
    >
      {children}
    </th>
  );
}

function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-1.5 py-1", className)}>{children}</td>;
}
