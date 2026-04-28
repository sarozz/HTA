// Browser-side fetch helper. All requests go through /api/proxy/* so the
// bearer token never reaches the client. SWR is the cache layer.

import type { Snapshot } from "./types";

export async function proxyFetch<T>(path: string): Promise<T> {
  const url = `/api/proxy${path.startsWith("/") ? path : `/${path}`}`;
  const r = await fetch(url, {
    method: "GET",
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!r.ok) {
    throw new Error(`proxy ${url} -> ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export const fetchSnapshot = () => proxyFetch<Snapshot>("/snapshot");
export const fetchHealth = () => proxyFetch<Snapshot["health"]>("/health");
export const fetchPositions = () => proxyFetch<Snapshot["positions"]>("/positions");
export const fetchEquity = () => proxyFetch<Snapshot["equity"]>("/equity?granularity=5m");
export const fetchSignals = () => proxyFetch<Snapshot["signals"]>("/signals?limit=200");
export const fetchRisk = () => proxyFetch<Snapshot["risk_events"]>("/risk_events?limit=50");
export const fetchTrades = () => proxyFetch<Snapshot["positions"]>("/trades?limit=500");
export const fetchStats = (window: string) =>
  proxyFetch<Snapshot["stats_24h"]>(`/stats?window=${window}`);
