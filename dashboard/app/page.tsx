"use client";

import { useEffect } from "react";
import useSWR from "swr";

import { EquityCard } from "@/components/EquityCard";
import { HeaderStrip } from "@/components/HeaderStrip";
import { OrderbookHeatmap } from "@/components/OrderbookHeatmap";
import { PositionCards } from "@/components/PositionCards";
import { PriceChartCard } from "@/components/PriceChartCard";
import { RiskGauges } from "@/components/RiskGauges";
import { SignalStream } from "@/components/SignalStream";
import { StatsCard } from "@/components/StatsCard";
import { TradesTable } from "@/components/TradesTable";
import { fetchSnapshot, proxyFetch } from "@/lib/api";
import { useDashboardStore } from "@/lib/store";
import type { Snapshot, Trade } from "@/lib/types";

export default function DashboardPage() {
  const setStatus = useDashboardStore((s) => s.setStatus);
  const recordUpdate = useDashboardStore((s) => s.recordUpdate);

  // Initial + polling fetch. SWR retries on failure; we mark "stale" while
  // a request is in flight after the first success.
  const { data, error, isLoading } = useSWR<Snapshot>(
    "snapshot",
    fetchSnapshot,
    {
      refreshInterval: 3_000,
      revalidateOnFocus: false,
      onSuccess: () => {
        setStatus("live");
        recordUpdate();
      },
      onError: () => setStatus("stale"),
    },
  );

  const { data: trades } = useSWR<Trade[]>(
    "trades",
    () => proxyFetch<Trade[]>("/trades?limit=500"),
    { refreshInterval: 5_000 },
  );

  useEffect(() => {
    if (isLoading) setStatus("connecting");
  }, [isLoading, setStatus]);

  const positions = data?.positions ?? [];
  const equity = data?.equity ?? [];
  const signals = data?.signals ?? [];
  const riskEvents = data?.risk_events ?? [];
  const stats24h = data?.stats_24h ?? null;
  const equityNow = equity.length > 0 ? parseFloat(equity[equity.length - 1]?.equity ?? "0") : 0;
  const symbols = Array.from(new Set(positions.map((p) => p.symbol))).sort();

  return (
    <main className="min-h-screen">
      <HeaderStrip snapshot={data ?? null} loading={isLoading && !data} />

      {error && !data && (
        <div className="bg-bear/15 text-bear text-sm px-4 py-2">
          unable to reach the bot API. retrying every 3s.
        </div>
      )}

      <div className="px-4 py-3 grid grid-cols-12 gap-3 auto-rows-min">
        {/* Row 1 — Equity & risk */}
        <div className="col-span-12 lg:col-span-8 h-[360px]">
          <EquityCard series={equity} loading={isLoading && !data} />
        </div>
        <div className="col-span-12 lg:col-span-4 h-[360px]">
          <RiskGauges
            stats={stats24h}
            positions={positions}
            riskEvents={riskEvents}
            equity={equityNow}
            loading={isLoading && !data}
          />
        </div>

        {/* Row 2 — Price chart + position cards */}
        <div className="col-span-12 lg:col-span-7 h-[420px]">
          <PriceChartCard symbols={symbols.length > 0 ? symbols : ["BTC", "ETH", "SOL"]} />
        </div>
        <div className="col-span-12 lg:col-span-5 h-[420px]">
          <PositionCards positions={positions} loading={isLoading && !data} />
        </div>

        {/* Row 3 — Orderbook + signals */}
        <div className="col-span-12 lg:col-span-5 h-[340px]">
          <OrderbookHeatmap symbol={symbols[0] ?? "BTC"} />
        </div>
        <div className="col-span-12 lg:col-span-7 h-[340px]">
          <SignalStream signals={signals} loading={isLoading && !data} />
        </div>

        {/* Row 4 — Trades + stats */}
        <div className="col-span-12 lg:col-span-8 h-[420px]">
          <TradesTable trades={trades ?? []} loading={!trades && isLoading} />
        </div>
        <div className="col-span-12 lg:col-span-4 h-[420px]">
          <StatsCard initial={stats24h} />
        </div>
      </div>
    </main>
  );
}
