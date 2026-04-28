// Server-side proxy for browser -> bot calls. Adds the bearer token
// from server env (HTA_API_TOKEN) so the browser never sees it.
//
// The browser calls /api/proxy/<endpoint> (e.g. /api/proxy/health). This
// route handler forwards GETs to ${HTA_API_URL}/api/<endpoint> with the
// bearer header attached, and returns the body verbatim.
//
// Authenticated users only — getServerSession gates the route.

import { NextResponse } from "next/server";
import { getServerSession } from "next-auth";

import { authOptions } from "@/lib/auth";

export const dynamic = "force-dynamic";

function botUrl(path: string[], search: string): string {
  const base = process.env.HTA_API_URL ?? "http://127.0.0.1:8080";
  const joined = path.map(encodeURIComponent).join("/");
  return `${base}/api/${joined}${search}`;
}

async function authorised(): Promise<boolean> {
  const session = await getServerSession(authOptions);
  return Boolean(session);
}

export async function GET(
  request: Request,
  { params }: { params: { path: string[] } },
) {
  if (!(await authorised())) {
    return NextResponse.json({ error: "unauthorised" }, { status: 401 });
  }
  const token = process.env.HTA_API_TOKEN ?? "";
  if (!token) {
    return NextResponse.json(
      { error: "server misconfigured: HTA_API_TOKEN not set" },
      { status: 500 },
    );
  }
  const url = new URL(request.url);
  const target = botUrl(params.path, url.search);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      // Bot is the source of truth; never cache.
      cache: "no-store",
    });
  } catch (err) {
    return NextResponse.json(
      { error: "bot unreachable", detail: String(err) },
      { status: 502 },
    );
  }
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}

// Hard-block any non-GET method; the dashboard is read-only.
export async function POST() {
  return NextResponse.json({ error: "read-only" }, { status: 405 });
}
export const PUT = POST;
export const PATCH = POST;
export const DELETE = POST;
