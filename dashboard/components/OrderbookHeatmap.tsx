"use client";

interface OrderbookHeatmapProps {
  symbol: string;
}

export function OrderbookHeatmap({ symbol }: OrderbookHeatmapProps) {
  // Real heatmap (D3 horizontal bars + colour intensity, 10fps throttle)
  // is the next-session deliverable. This placeholder keeps the layout
  // honest at production density.
  return (
    <section className="card p-4 h-full flex flex-col">
      <header className="flex items-center justify-between mb-2">
        <span className="label">Orderbook · {symbol}</span>
        <span className="text-text2 text-xs">20 levels</span>
      </header>
      <div className="flex-1 grid place-items-center text-text2 text-sm border border-dashed border-border rounded">
        D3 heatmap + 10fps throttle land here next session
      </div>
    </section>
  );
}
