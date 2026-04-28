"use client";

import type { Position } from "@/lib/types";
import { cn } from "@/lib/cn";
import { holdDuration, money, pct, size } from "@/lib/format";

interface PositionCardsProps {
  positions: Position[];
  loading: boolean;
}

export function PositionCards({ positions, loading }: PositionCardsProps) {
  const open = positions.filter((p) => parseFloat(p.size) !== 0);

  return (
    <section className="card p-3 h-full overflow-auto space-y-2">
      <header className="px-1">
        <span className="label">Positions</span>
      </header>
      {loading ? (
        <div className="skeleton h-32 w-full" />
      ) : open.length === 0 ? (
        <div className="text-center py-12 text-text2 text-sm">
          no open positions
        </div>
      ) : (
        open.map((p) => <PositionCard key={p.symbol} position={p} />)
      )}
    </section>
  );
}

function PositionCard({ position }: { position: Position }) {
  const sz = parseFloat(position.size);
  const long = sz > 0;
  const entry = parseFloat(position.entry_price);
  const mark = parseFloat(position.mark_price);
  const liq = position.liq_price ? parseFloat(position.liq_price) : null;
  const notional = Math.abs(sz * mark);
  const upnl = sz * (mark - entry);

  return (
    <div className="card-raised p-3 space-y-2">
      <div className="flex items-baseline justify-between">
        <div className="flex items-center gap-2">
          <span className="font-semibold tracking-tight">{position.symbol}</span>
          <span
            className={cn(
              "pill",
              long ? "bg-bull/15 text-bull" : "bg-bear/15 text-bear",
            )}
          >
            {long ? "LONG" : "SHORT"}
          </span>
        </div>
        <span className="num text-sm text-text1">
          {money(notional)} · {size(Math.abs(sz))} {position.symbol}
        </span>
      </div>
      <dl className="grid grid-cols-3 gap-x-2 text-xs">
        <Field label="Entry" value={money(entry)} />
        <Field label="Mark" value={money(mark)} />
        <Field
          label="uPnL"
          value={money(upnl, { sign: true })}
          tone={upnl >= 0 ? "bull" : "bear"}
        />
        <Field
          label="Liq px"
          value={liq != null ? money(liq) : "—"}
          tone={liq != null && Math.abs(mark - liq) / mark < 0.08 ? "bear" : "default"}
        />
        <Field
          label="Liq dist"
          value={liq != null ? pct(Math.abs(mark - liq) / mark) : "—"}
        />
        <Field label="Held" value={holdDuration(position.ts)} />
      </dl>
    </div>
  );
}

function Field({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "bull" | "bear";
}) {
  return (
    <div>
      <div className="label leading-tight">{label}</div>
      <div
        className={cn(
          "num",
          tone === "bull" && "text-bull",
          tone === "bear" && "text-bear",
        )}
      >
        {value}
      </div>
    </div>
  );
}
