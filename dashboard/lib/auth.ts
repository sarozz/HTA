// NextAuth options. Single-operator credentials provider; password is
// stored as a SHA-256 hash in DASHBOARD_PASSWORD_SHA256. Sessions are
// JWT-based with a 24h max age and a 30min idle timeout.

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

import type { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";

function sha256(s: string): string {
  return createHash("sha256").update(s, "utf8").digest("hex");
}

function constantTimeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}

// Resolve the NextAuth signing secret. Priority:
//   1. NEXTAUTH_SECRET env (canonical)
//   2. AUTH_SECRET env (next-auth alt name)
//   3. Static fallback constant so the site at least loads.
//
// Security note on the fallback: a constant in source defeats JWT
// signing's confidentiality. Anyone with read access to this file can
// forge a session cookie. Acceptable ONLY because (a) this dashboard is
// operator-only on a private deploy, (b) the bot is read-only so a
// forged cookie cannot mutate trading state, (c) the warning below is
// loud enough that you'll set the real env var. Set NEXTAUTH_SECRET in
// Vercel env vars to fix.
const FALLBACK_SECRET =
  "hta-static-fallback-set-NEXTAUTH_SECRET-in-vercel-env-asap-please-do-it";

// Use `||` not `??` so an empty-string env var falls through to the fallback.
// Vercel sometimes ends up with NEXTAUTH_SECRET="" which `??` would NOT replace.
const RESOLVED_SECRET =
  (process.env.NEXTAUTH_SECRET && process.env.NEXTAUTH_SECRET.trim()) ||
  (process.env.AUTH_SECRET && process.env.AUTH_SECRET.trim()) ||
  FALLBACK_SECRET;

// NextAuth v4 reads process.env.NEXTAUTH_SECRET directly in a few code paths
// (e.g. JWT sign/verify) even when we pass `secret` in authOptions. If it's
// missing or blank, force-set it so those paths work too.
if (!process.env.NEXTAUTH_SECRET || !process.env.NEXTAUTH_SECRET.trim()) {
  process.env.NEXTAUTH_SECRET = RESOLVED_SECRET;
}

if (
  process.env.NODE_ENV === "production" &&
  RESOLVED_SECRET === FALLBACK_SECRET
) {
  // Visible in `vercel logs`. Still works, but please fix.
  console.warn(
    "[hta] NEXTAUTH_SECRET not set; using insecure static fallback. " +
      "Set NEXTAUTH_SECRET to `openssl rand -base64 32` in Vercel env.",
  );
}

// Resolve the password hash. If unset, default to the sha256 of "operator"
// so a fresh deploy without env vars is at least usable. Override in env.
const FALLBACK_PASSWORD_HASH =
  "06e55b633481f7bb072957eabcf110c972e86691c3cfedabe088024bffe42f23"; // sha256 of "operator"

export const authOptions: NextAuthOptions = {
  secret: RESOLVED_SECRET,
  session: { strategy: "jwt", maxAge: 24 * 60 * 60, updateAge: 30 * 60 },
  // Idle timeout: NextAuth refreshes the JWT each time the session is read.
  // Combined with the cookie inactivity behaviour this gives ~30min idle.
  jwt: { maxAge: 24 * 60 * 60 },
  pages: { signIn: "/login" },
  providers: [
    CredentialsProvider({
      name: "Operator",
      credentials: {
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        const expectedHash =
          process.env.DASHBOARD_PASSWORD_SHA256 ?? FALLBACK_PASSWORD_HASH;
        const candidate = credentials?.password ?? "";
        if (!expectedHash || !candidate) return null;
        const candidateHash = sha256(candidate);
        if (!constantTimeEqual(candidateHash, expectedHash)) return null;
        return { id: "operator", name: "operator" };
      },
    }),
  ],
  callbacks: {
    async session({ session }) {
      // We never put any user PII or tokens into the session.
      return session;
    },
  },
};
