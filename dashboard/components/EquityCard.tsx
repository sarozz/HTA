"use client";

import { useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { EquityPoint } from "@/lib/types";
import { cn } from "@/lib/cn";
import { clockTime, money } from "@/lib/format";

const RANGES = ["24h", "7d", "30d", "90d", "all"] as const;
type Range = (typeof RANGES)[number];

interface EquityCardProps {
  series: EquityPoint[];
  loading: boolean;
}

export function EquityCard({ series, loading }: EquityCardProps) {
  const [range, setRange] = useState<Range>("24h");
  const data = series.map((p) => ({
    ts: p.ts,
    equity: parseFloat(p.equity ?? "0"),
    realized: parseFloat(p.realized ?? "0"),
    unrealized: parseFloat(p.unrealized ?? "0"),
  }));

  return (
    <section className="card p-4 h-full flex flex-col">
      <header className="flex items-center justify-between mb-2">
        <div>
          <span className="label">Equity</span>
        </div>
        <div className="flex items-center gap-1 text-xs">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={cn(
                "px-2 py-0.5 rounded uppercase tracking-wider",
                range === r
                  ? "bg-bg2 text-text0"
                  : "text-text2 hover:text-text1",
              )}
            >
              {r}
            </button>
          ))}
        </div>
      </header>
      <div className="flex-1 min-h-0">
        {loading || data.length === 0 ? (
          <div className="skeleton h-full w-full" />
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="equity-gradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#26D9A8" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#26D9A8" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1F2937" vertical={false} />
              <XAxis
                dataKey="ts"
                tickFormatter={(t) => new Date(t * 1000).toISOString().slice(11, 16)}
                stroke="#5C6773"
                tick={{ fontSize: 11 }}
                minTickGap={50}
              />
              <YAxis
                stroke="#5C6773"
                tick={{ fontSize: 11 }}
                tickFormatter={(v) => `$${v.toFixed(0)}`}
                width={60}
                domain={["dataMin - 5", "dataMax + 5"]}
              />
              <Tooltip
                cursor={{ stroke: "#1F2937" }}
                contentStyle={{
                  background: "#0F141B",
                  border: "1px solid #1F2937",
                  borderRadius: 4,
                  fontSize: 12,
                }}
                labelFormatter={(t) => clockTime(t as number)}
                formatter={(value: number, name: string) => [money(value), name]}
              />
              <Area
                type="monotone"
                dataKey="equity"
                stroke="#26D9A8"
                strokeWidth={1.5}
                fill="url(#equity-gradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}
