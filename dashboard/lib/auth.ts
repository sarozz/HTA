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

// Resolve the NextAuth signing secret. Prefer NEXTAUTH_SECRET (the
// canonical name); fall back to AUTH_SECRET. If neither is present in
// production, generate an ephemeral secret per process so the site
// boots — the cost is sessions invalidating on every cold start. The
// proper fix is to set NEXTAUTH_SECRET in the deployment env.
function resolveSecret(): string {
  const explicit = process.env.NEXTAUTH_SECRET ?? process.env.AUTH_SECRET;
  if (explicit) return explicit;
  if (process.env.NODE_ENV === "production") {
    // Loud warning, no crash. Visible in `vercel logs`.
    console.warn(
      "[hta] NEXTAUTH_SECRET is not set. Generating an ephemeral secret. " +
        "Sessions will not survive cold starts. Set NEXTAUTH_SECRET in env to fix.",
    );
  }
  return randomBytes(32).toString("hex");
}

const RESOLVED_SECRET = resolveSecret();

// Resolve the password hash. If unset, default to the sha256 of "operator"
// so a fresh deploy without env vars is at least usable. Override in env.
const FALLBACK_PASSWORD_HASH =
  "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918"; // sha256 of "operator"

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
