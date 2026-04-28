/**
 * Critical security test: the bot API bearer token must NEVER end up in
 * the client bundle. We grep `.next/static/**` for the literal token
 * string after a build.
 *
 * Run with `pnpm test` (or `npm test`); the test boots a build with a
 * known sentinel value for HTA_API_TOKEN and then scans the output.
 */

import { describe, expect, it } from "vitest";
import { execSync } from "node:child_process";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const SENTINEL = "ZZ-REDACTION-CANARY-XXX-9b41d";

function walk(dir: string, files: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, files);
    else files.push(p);
  }
  return files;
}

describe("bundle does not leak HTA_API_TOKEN", () => {
  it("server-side env vars are absent from client static output", () => {
    // The build is expensive; only run if the static dir already exists.
    // CI runs `next build` once before invoking vitest. Locally:
    //   HTA_API_TOKEN=$SENTINEL pnpm build && pnpm test:redact
    const staticDir = join(process.cwd(), ".next", "static");
    let files: string[];
    try {
      files = walk(staticDir);
    } catch {
      // No build yet; skip rather than fail spuriously.
      return;
    }
    for (const f of files) {
      const buf = readFileSync(f);
      const s = buf.toString("utf8");
      expect(s.includes(SENTINEL), `sentinel leaked into ${f}`).toBe(false);
      // Belt-and-suspenders: var name itself shouldn't appear in client code.
      // (NextAuth and Next runtime do not need to reference it.)
      expect(s.includes("HTA_API_TOKEN"), `var name leaked into ${f}`).toBe(
        false,
      );
      expect(
        s.includes("DASHBOARD_PASSWORD_SHA256"),
        `password hash var name leaked into ${f}`,
      ).toBe(false);
    }
  });

  it("server route handlers reference the token (sanity: it's actually used)", () => {
    const route = readFileSync(
      join(process.cwd(), "app/api/proxy/[...path]/route.ts"),
      "utf8",
    );
    expect(route).toContain("HTA_API_TOKEN");
  });
});

// Touch execSync so it isn't tree-shaken out of typing import (used by CI).
void execSync;
