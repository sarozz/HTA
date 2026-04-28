// Copies the latest backtest report and equity-curve PNG from the
// repo's reports/ directory into dashboard/data/ and dashboard/public/
// before each Next.js build. Tolerates missing files: if reports
// haven't been generated yet, the dashboard renders a placeholder.

import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const dashboardRoot = resolve(__dirname, "..");
const repoRoot = resolve(dashboardRoot, "..");

const reportSrc = resolve(repoRoot, "reports", "strategy_a_backtest.md");
const pngSrc = resolve(repoRoot, "reports", "strategy_a_equity_curve.png");

const dataDir = resolve(dashboardRoot, "data");
const publicDir = resolve(dashboardRoot, "public");
mkdirSync(dataDir, { recursive: true });
mkdirSync(publicDir, { recursive: true });

const reportDest = resolve(dataDir, "strategy_a_backtest.md");
const pngDest = resolve(publicDir, "strategy_a_equity_curve.png");

if (existsSync(reportSrc)) {
  copyFileSync(reportSrc, reportDest);
  console.log(`copied report: ${reportSrc} -> ${reportDest}`);
} else {
  writeFileSync(
    reportDest,
    "# No backtest report available yet\n\nRun `python -m scripts.run_backtest` (or the synthetic demo) to generate one.\n",
  );
  console.warn(`report missing at ${reportSrc}; wrote placeholder`);
}

if (existsSync(pngSrc)) {
  copyFileSync(pngSrc, pngDest);
  console.log(`copied chart : ${pngSrc} -> ${pngDest}`);
} else {
  console.warn(`chart missing at ${pngSrc}; dashboard will hide the image`);
}

const meta = {
  generated_at: new Date().toISOString(),
  has_chart: existsSync(pngSrc),
  has_report: existsSync(reportSrc),
};
writeFileSync(resolve(dataDir, "meta.json"), JSON.stringify(meta, null, 2));
console.log(`wrote meta : ${JSON.stringify(meta)}`);
