"use client";

import { cn } from "@/lib/cn";

interface StatusDotProps {
  status: "live" | "halted" | "stale" | "connecting";
}

const colors = {
  live: "bg-bull",
  halted: "bg-halt",
  stale: "bg-warn",
  connecting: "bg-text2",
};

export function StatusDot({ status }: StatusDotProps) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-full",
        colors[status],
        status === "live" && "shadow-[0_0_8px] shadow-bull/60",
        status === "halted" && "animate-pulse shadow-[0_0_10px] shadow-halt/80",
      )}
      aria-hidden
    />
  );
}
