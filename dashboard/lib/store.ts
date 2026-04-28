// Minimal Zustand store. SWR holds the actual data per endpoint; this
// store just tracks the connection state pill and the active symbol.

import { create } from "zustand";

export type LiveStatus = "connecting" | "live" | "stale" | "halted";

interface DashboardState {
  active_symbol: string;
  status: LiveStatus;
  last_update_ts: number;
  setActiveSymbol: (s: string) => void;
  setStatus: (s: LiveStatus) => void;
  recordUpdate: () => void;
}

export const useDashboardStore = create<DashboardState>((set) => ({
  active_symbol: "BTC",
  status: "connecting",
  last_update_ts: 0,
  setActiveSymbol: (s) => set({ active_symbol: s }),
  setStatus: (s) => set({ status: s }),
  recordUpdate: () => set({ last_update_ts: Date.now() / 1000 }),
}));
