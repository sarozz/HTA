import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "HTA — Trading Dashboard",
  description: "Read-only operator dashboard for the HTA Hyperliquid trading bot.",
  robots: { index: false, follow: false },
};

export const viewport = {
  themeColor: "#0A0E14",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="antialiased min-h-screen">{children}</body>
    </html>
  );
}
