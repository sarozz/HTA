import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Meta {
  generated_at: string;
  has_chart: boolean;
  has_report: boolean;
}

const dataDir = path.join(process.cwd(), "data");

function loadReport(): string {
  const reportPath = path.join(dataDir, "strategy_a_backtest.md");
  if (!existsSync(reportPath)) {
    return "# No backtest report available yet\n";
  }
  return readFileSync(reportPath, "utf-8");
}

function loadMeta(): Meta {
  const metaPath = path.join(dataDir, "meta.json");
  if (!existsSync(metaPath)) {
    return {
      generated_at: new Date().toISOString(),
      has_chart: false,
      has_report: false,
    };
  }
  return JSON.parse(readFileSync(metaPath, "utf-8")) as Meta;
}

// Strip the chart image link out of the markdown body — we render the
// chart in its own card above the report so we don't want it twice. The
// regex matches the markdown image syntax for our specific filename.
function stripChartImage(body: string): string {
  return body.replace(/!\[[^\]]*]\(strategy_a_equity_curve\.png\)\s*\n?/g, "");
}

export default function Page() {
  const meta = loadMeta();
  const reportRaw = loadReport();
  const reportBody = stripChartImage(reportRaw);

  return (
    <main>
      <header className="hero">
        <h1>HTA — Hyperliquid Algo Trading</h1>
        <p className="subtitle">
          Read-only status dashboard. The trading bot itself runs on a
          persistent host (not Vercel); this page renders the latest backtest
          report committed to the repo.
        </p>
        <div className="meta">
          <span>
            <strong>Build:</strong> {meta.generated_at}
          </span>
          <span>
            <strong>Source:</strong>{" "}
            <a href="https://github.com/sarozz/hta">sarozz/hta</a>
          </span>
        </div>
      </header>

      <section className="status-grid">
        <div className="status-card">
          <div className="label">Trading bot</div>
          <div className="value muted">Not yet deployed (testnet only)</div>
        </div>
        <div className="status-card">
          <div className="label">Network</div>
          <div className="value">Testnet</div>
        </div>
        <div className="status-card">
          <div className="label">Latest backtest</div>
          <div className="value">{meta.has_report ? "available" : "none"}</div>
        </div>
        <div className="status-card">
          <div className="label">Halt status</div>
          <div className="value muted">n/a (bot not running)</div>
        </div>
      </section>

      {meta.has_chart && (
        <section className="chart">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/strategy_a_equity_curve.png"
            alt="Strategy A equity curve"
          />
          <p className="caption">
            Equity curve from the most recent backtest run. See the report
            below for headline metrics and configuration.
          </p>
        </section>
      )}

      <article className="report">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{reportBody}</ReactMarkdown>
      </article>

      <footer>
        Static read-only dashboard. Live position and journal data require the
        bot to be running and reachable; that pipeline is not wired up yet.
      </footer>
    </main>
  );
}
