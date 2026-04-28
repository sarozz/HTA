# HTA dashboard

Read-only status page for the HTA trading bot. Built with Next.js (App
Router) and deployed on Vercel.

The trading bot itself (`src/`) is a long-running Python process and
**cannot run on Vercel** — it needs persistent connections, background
workers, and on-disk state. This dashboard is a static site that:

- Renders the latest backtest report committed to `reports/` in the repo
- Embeds the equity-curve PNG as a chart card
- Shows a placeholder for live-bot status (not wired up yet)

## Vercel project settings

When you create / configure the Vercel project for this repo, set:

| Setting | Value |
| --- | --- |
| Framework Preset | Next.js (auto-detected) |
| **Root Directory** | `dashboard` |
| Build Command | `npm run build` (default) |
| Output Directory | `.next` (default) |
| Install Command | `npm install` (default) |
| Node.js Version | 22.x (matches local) |

The **Root Directory** setting is the important one — without it, Vercel
will try to build from the repo root and fail because the root is a
Python project, not a Node project.

### Branch behaviour

By default Vercel:
- Builds and deploys the **production** branch (usually `main`) to the
  primary domain
- Builds **preview** deploys for every other branch / PR

If you want this branch (`claude/setup-hyperliquid-trading-eJAME`) to
go to production, either merge it to `main` or change the Vercel
project's Production Branch setting.

## Local development

```bash
cd dashboard
npm install
npm run dev   # http://localhost:3000
```

The `predev` and `prebuild` scripts copy `reports/strategy_a_backtest.md`
and `reports/strategy_a_equity_curve.png` from the repo into
`dashboard/data/` and `dashboard/public/` so the page has data to render.
If those files don't exist (no backtest run yet), the page falls back to
a placeholder; nothing crashes.

To regenerate the report, run from the repo root:

```bash
python -m scripts.run_backtest_synthetic    # synthetic 60-day demo
# or, with network access to Hyperliquid:
python -m scripts.run_backtest               # real 180-day pull
```

Then re-run `npm run build` (or the prebuild step alone:
`node scripts/copy-data.mjs`) to refresh the dashboard's copy.

## What this dashboard is NOT

- It does NOT show live positions, fills, or P&L. The bot is not yet
  deployed and there is no API for the dashboard to query.
- It does NOT trigger trades. Read-only by design.
- It does NOT host the bot. The bot needs a persistent host (your
  laptop, a VPS, Fly.io, Railway, Render, etc.).

When the bot is running and reachable, a future iteration can add
`/api/*` routes (Vercel serverless functions) that proxy to the bot's
HTTP API or read from a hosted database (Neon, Supabase, etc.) the bot
writes to.
