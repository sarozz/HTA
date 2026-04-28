"use client";

import { useEffect, useState } from "react";

import type { Snapshot } from "@/lib/types";
import { cn } from "@/lib/cn";
import { delta, money, relTime } from "@/lib/format";
import { useDashboardStore } from "@/lib/store";

import { StatusDot } from "./StatusDot";

interface HeaderStripProps {
  snapshot: Snapshot | null;
  loading: boolean;
}

export function HeaderStrip({ snapshot, loading }: HeaderStripProps) {
  const status = useDashboardStore((s) => s.status);

  const equity = snapshot?.equity ?? [];
  const latest = equity.at(-1);
  const equityNow = latest?.equity ?? null;
  // 24h ago = first point of the snapshot's 5m series, which queries.py
  // sets to ~288 points (~24h).
  const equity24hAgo = equity[0]?.equity ?? null;
  const equityDelta = delta(equityNow, equity24hAgo);

  const realized = latest?.realized ?? null;
  const positionsCount = snapshot?.positions?.length ?? 0;
  const halted = snapshot?.health?.heartbeat?.halted ?? false;
  const mode = snapshot?.health?.mode ?? "unknown";

  const lastBeat = snapshot?.health?.heartbeat?.ts ?? null;
  const beatStatus = useBeatStaleness(lastBeat);

  const banner = halted
    ? "HALTED"
    : mode === "mainnet"
      ? "LIVE MAINNET"
      : mode === "testnet"
        ? "LIVE TESTNET"
        : "OFFLINE";

  return (
    <header
      className={cn(
        "sticky top-0 z-30 h-14 px-4 grid items-center bg-bg1 border-b border-border",
        "grid-cols-[auto_1fr_auto_auto_auto_auto] gap-x-4",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="font-semibold tracking-tight text-text0">HTA</span>
        <span className="text-text2 text-xs">/dashboard</span>
      </div>

      <div className="flex items-center gap-2">
        <StatusDot status={halted ? "halted" : status} />
        <span
          className={cn(
            "text-xs font-medium uppercase tracking-wider",
            halted ? "text-halt" : "text-text0",
          )}
        >
          {banner}
        </span>
        {status === "stale" && !halted && (
          <span className="pill bg-warn/15 text-warn">reconnecting…</span>
        )}
      </div>

      <Stat
        label="Equity"
        value={loading ? null : money(equityNow)}
        delta={equityDelta.abs}
        deltaSuffix="24h"
      />
      <Stat
        label="Today P&L"
        value={loading ? null : money(realized, { sign: true })}
      />
      <Stat
        label="Positions"
        value={loading ? null : String(positionsCount)}
      />
      <div className="text-right">
        <div className="label">Heartbeat</div>
        <div
          className={cn(
            "num text-sm",
            beatStatus === "stale" ? "text-warn" : "text-text1",
          )}
        >
          {relTime(lastBeat)}
        </div>
      </div>
    </header>
  );
}

function Stat({
  label,
  value,
  delta,
  deltaSuffix,
}: {
  label: string;
  value: string | null;
  delta?: number | null;
  deltaSuffix?: string;
}) {
  const positive = (delta ?? 0) > 0;
  const negative = (delta ?? 0) < 0;
  return (
    <div className="text-right min-w-[7rem]">
      <div className="label">{label}</div>
      <div className="num text-lg leading-none mt-0.5">
        {value ?? <span className="skeleton inline-block h-4 w-20" />}
      </div>
      {delta != null && (
        <div
          className={cn(
            "num text-xs mt-0.5",
            positive && "text-bull",
            negative && "text-bear",
            !positive && !negative && "text-text2",
          )}
        >
          {positive ? "+" : negative ? "" : ""}
          {money(delta)} {deltaSuffix}
        </div>
      )}
    </div>
  );
}

function useBeatStaleness(ts: number | null): "live" | "stale" {
  const [now, setNow] = useState<number>(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, []);
  if (ts == null) return "stale";
  return now - ts > 5 ? "stale" : "live";
}
