"use client";

import type { Trade } from "@/lib/types";
import { cn } from "@/lib/cn";
import { clockTime, money, size } from "@/lib/format";

interface TradesTableProps {
  trades: Trade[];
  loading: boolean;
}

export function TradesTable({ trades, loading }: TradesTableProps) {
  const closed = trades.filter((t) => t.opens_or_closes !== "open");
  const all = trades; // show all rows; real virtualisation in a follow-up

  return (
    <section className="card p-3 h-full flex flex-col">
      <header className="flex items-center justify-between mb-2 px-1">
        <span className="label">
          Trades · {closed.length} closed · {trades.length - closed.length} open
        </span>
        <button
          onClick={() => downloadCsv(trades)}
          className="text-text2 hover:text-text1 text-xs uppercase tracking-wider"
        >
          export csv
        </button>
      </header>
      <div className="flex-1 min-h-0 overflow-auto">
        {loading ? (
          <div className="skeleton h-32 w-full" />
        ) : all.length === 0 ? (
          <div className="text-text2 text-sm text-center py-8">no trades yet</div>
        ) : (
          <table className="w-full text-xs">
            <thead className="text-text2 sticky top-0 bg-bg1">
              <tr>
                <Th className="w-24 text-left">Time</Th>
                <Th className="w-12 text-left">Sym</Th>
                <Th className="w-12 text-left">Side</Th>
                <Th className="w-8 text-left">Strat</Th>
                <Th className="w-20 text-right">Size</Th>
                <Th className="w-20 text-right">Price</Th>
                <Th className="w-16 text-right">Fee</Th>
                <Th className="w-20 text-right">P&L</Th>
                <Th className="text-left">Note</Th>
              </tr>
            </thead>
            <tbody>
              {all.slice(0, 500).map((t, i) => {
                const pnl = t.pnl != null ? parseFloat(t.pnl) : null;
                return (
                  <tr
                    key={`${t.ts}-${t.oid ?? i}`}
                    className="border-t border-border/50 hover:bg-bg2/50"
                  >
                    <Td className="num text-text2">
                      {clockTime(t.ts).slice(11, 19)}
                    </Td>
                    <Td>{t.symbol}</Td>
                    <Td
                      className={cn(
                        "uppercase",
                        t.side === "buy" ? "text-bull" : "text-bear",
                      )}
                    >
                      {t.side}
                    </Td>
                    <Td className="text-text1">
                      {t.strategy === "mean_reversion"
                        ? "A"
                        : t.strategy === "funding_capture"
                          ? "B"
                          : "?"}
                    </Td>
                    <Td className="num text-right">{size(t.size, 6)}</Td>
                    <Td className="num text-right">{money(t.price)}</Td>
                    <Td className="num text-right text-text2">
                      {t.fee != null ? money(t.fee) : "—"}
                    </Td>
                    <Td
                      className={cn(
                        "num text-right",
                        pnl == null
                          ? "text-text2"
                          : pnl >= 0
                            ? "text-bull"
                            : "text-bear",
                      )}
                    >
                      {pnl == null ? "—" : money(pnl, { sign: true })}
                    </Td>
                    <Td className="text-text1 truncate">
                      {t.opens_or_closes ?? ""}
                    </Td>
                  </tr>
                );
              })}
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
      className={cn("uppercase tracking-wider font-medium px-1.5 py-1.5", className)}
    >
      {children}
    </th>
  );
}

function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-1.5 py-1", className)}>{children}</td>;
}

function downloadCsv(trades: Trade[]): void {
  if (trades.length === 0) return;
  const header = ["ts", "symbol", "side", "strategy", "size", "price", "fee", "pnl"];
  const rows = trades.map((t) => [
    new Date(t.ts * 1000).toISOString(),
    t.symbol,
    t.side,
    t.strategy,
    t.size,
    t.price,
    t.fee ?? "",
    t.pnl ?? "",
  ]);
  const csv = [header, ...rows].map((r) => r.join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `hta-trades-${Date.now()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
