import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "HTA — Hyperliquid Algo Trading",
  description:
    "Read-only status dashboard for the HTA trading bot (mean reversion + funding capture).",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
