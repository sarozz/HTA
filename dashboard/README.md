# HTA dashboard (Next.js)

Real-time read-only operator dashboard for the HTA Hyperliquid trading
bot. Bloomberg-density layout, dark-first, single page, no
navigation. Connects to the bot's `dashboard_api` (FastAPI/uvicorn).

## Stack

- Next.js 14 (App Router) + TypeScript strict
- Tailwind CSS with a custom dark theme (token mapping in
  `tailwind.config.ts`)
- NextAuth credentials provider (single-operator password)
- SWR for REST polling (3s default)
- Zustand for client UI state
- Recharts for the equity curve
- Framer Motion for transitions (subtle)
- Vitest for tests (bundle-redaction is the load-bearing one)

## Local dev — offline (recommended for UI work)

The mock bot API and the dashboard run together via docker-compose:

```bash
cd dashboard
docker compose up
# open http://localhost:3000
# login password: operator
```

If you don't have docker, run them separately:

```bash
# terminal 1 — mock bot API
DASHBOARD_API_TOKEN=local-dev-token-do-not-use-in-prod \
  uvicorn mock_api.server:app --host 127.0.0.1 --port 8080

# terminal 2 — Next.js
cp .env.example .env.local
pnpm install   # or npm
pnpm dev       # http://localhost:3000
```

## Local dev — against the real bot

Point `HTA_API_URL` at the bot host and use the real bearer token. With
Caddy in front of the bot, that's typically `https://dashboard.bot.example.com`:

```bash
export HTA_API_URL=https://dashboard.bot.example.com
export HTA_API_TOKEN=<the long random token from /etc/hta/dashboard.env>
pnpm dev
```

## Auth model

There is **one operator password**, stored as its SHA-256 hash in
`DASHBOARD_PASSWORD_SHA256`. To rotate:

```bash
echo -n "your-new-password" | shasum -a 256 | awk '{print $1}'
# put that in .env.local (or Vercel env)
```

`NEXTAUTH_SECRET` should be a long random string, generated once
per deployment:

```bash
openssl rand -base64 32
```

The bot's bearer token (`HTA_API_TOKEN`) lives in **server env only**.
Browser code never sees it — every call goes through `/api/proxy/*`
which is gated by NextAuth and adds the bearer header server-side. The
`test/redact.test.ts` Vitest test scans the built static bundle for any
trace of the token name and fails the build if anything leaks.

## Vercel deployment

The Vercel project should:

| Setting | Value |
| --- | --- |
| **Root Directory** | `dashboard` |
| Framework Preset | Next.js (auto) |
| Region | `sin1` (Singapore — Asia, configured in `vercel.json`) |
| Node.js Version | 22.x |

Required env vars (Production + Preview):

| Var | What |
| --- | --- |
| `HTA_API_URL` | `https://dashboard.bot.example.com` (your bot+Caddy URL) |
| `HTA_API_TOKEN` | bearer token (matches the bot's `DASHBOARD_API_TOKEN`) |
| `NEXTAUTH_URL` | `https://dashboard.example.com` (Vercel-assigned URL or custom domain) |
| `NEXTAUTH_SECRET` | `openssl rand -base64 32` |
| `DASHBOARD_PASSWORD_SHA256` | sha256 of operator password |

Push to the branch Vercel watches; deploy fires automatically. The
dashboard will not work until the bot host is reachable from Vercel —
verify with `curl -H "Authorization: Bearer <token>" $HTA_API_URL/api/health`
from any internet host first.

## Deliberate gaps (next session)

This session ships the foundation + Rows 1 / 4 fully built and Rows
2 / 3 wired with placeholders that hold their grid space at the
correct density. The next session fills in:

- Live price chart (lightweight-charts, /ws tick channel)
- Orderbook heatmap (D3, 10fps throttle)
- Trade markers on the price chart
- WS data flow (currently using SWR polling at 3s; spec calls for WS
  with snapshot + delta and a polling fallback. The polling fallback
  is what's running today)
- TanStack Virtual on the trades table (currently caps display at 500
  rows — fine for now, will overflow at 10k+ rows on a long session)
- Playwright smoke
- Bundle-size budget assertion (< 350KB gzipped)

## Hard constraints (don't relax these without thought)

- **Read-only.** No POST/PUT/PATCH/DELETE in the proxy or anywhere in
  the UI. If you ever want a halt button: refuse, and send a Telegram
  command instead.
- **No bearer token in the browser.** Every fetch goes through
  `/api/proxy/*`. The redaction test fails the build if anything leaks.
- **No NEXT_PUBLIC_*** env vars touch tokens / URLs. Add them only for
  truly-public values (none today).
- **Read-only WS.** The WS client (when wired next session) refuses to
  send anything other than `{action: "subscribe", ...}`. The bot enforces
  this server-side too.

## Tests

```bash
# Build first; the redaction test scans .next/static/**
HTA_API_TOKEN=ZZ-REDACTION-CANARY-XXX-9b41d pnpm build
pnpm test
```

If the redaction test fails, the bundle is leaking server env. Fix
before merging.
