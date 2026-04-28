// Formatting helpers. Numbers are right-aligned, tabular-nums.
// Money formatting keeps two decimals; percent shows two decimals;
// bps shows zero. Returns "—" for null/NaN so the UI never has gaps.

export const DASH = "—";

function num(x: unknown): number | null {
  if (x === null || x === undefined) return null;
  const n = typeof x === "number" ? x : parseFloat(String(x));
  if (!Number.isFinite(n)) return null;
  return n;
}

export function money(x: unknown, opts?: { sign?: boolean }): string {
  const n = num(x);
  if (n === null) return DASH;
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : opts?.sign ? "+" : "";
  return `${sign}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function pct(x: unknown, decimals = 2): string {
  const n = num(x);
  if (n === null) return DASH;
  return `${(n * 100).toFixed(decimals)}%`;
}

export function bps(x: unknown): string {
  const n = num(x);
  if (n === null) return DASH;
  return `${(n * 10_000).toFixed(0)} bps`;
}

export function size(x: unknown, decimals = 4): string {
  const n = num(x);
  if (n === null) return DASH;
  return n.toFixed(decimals);
}

export function delta(curr: unknown, prev: unknown): {
  abs: number | null;
  pct: number | null;
} {
  const c = num(curr);
  const p = num(prev);
  if (c === null || p === null) return { abs: null, pct: null };
  return { abs: c - p, pct: p === 0 ? null : (c - p) / p };
}

export function relTime(ts: number | null | undefined): string {
  if (ts == null) return DASH;
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 0) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

export function clockTime(ts: number | null | undefined): string {
  if (ts == null) return DASH;
  const d = new Date(ts * 1000);
  return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

export function holdDuration(ts: number | null | undefined): string {
  if (ts == null) return DASH;
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  const h = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const s = String(seconds % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}
