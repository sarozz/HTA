"use client";

import { useDashboardStore } from "@/lib/store";

interface PriceChartCardProps {
  symbols: string[];
}

export function PriceChartCard({ symbols }: PriceChartCardProps) {
  const active = useDashboardStore((s) => s.active_symbol);
  const setActive = useDashboardStore((s) => s.setActiveSymbol);

  return (
    <section className="card p-4 h-full flex flex-col">
      <header className="flex items-center justify-between mb-2">
        <span className="label">Price · {active}-PERP</span>
        <div className="flex items-center gap-1 text-xs">
          {symbols.length === 0 ? (
            <span className="text-text2">no symbols</span>
          ) : (
            symbols.map((s) => (
              <button
                key={s}
                onClick={() => setActive(s)}
                className={
                  s === active
                    ? "px-2 py-0.5 rounded bg-bg2 text-text0"
                    : "px-2 py-0.5 rounded text-text2 hover:text-text1"
                }
              >
                {s}
              </button>
            ))
          )}
        </div>
      </header>
      <div className="flex-1 grid place-items-center text-text2 text-sm border border-dashed border-border rounded">
        live candles + entry/exit markers land here next session
        <br />
        <span className="text-text2">
          (lightweight-charts wired against /ws tick channel)
        </span>
      </div>
    </section>
  );
}
