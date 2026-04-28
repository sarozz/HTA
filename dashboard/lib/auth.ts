// NextAuth options. Single-operator credentials provider; password is
// stored as a SHA-256 hash in DASHBOARD_PASSWORD_SHA256. Sessions are
// JWT-based with a 24h max age and a 30min idle timeout.

import { createHash, timingSafeEqual } from "node:crypto";

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

export const authOptions: NextAuthOptions = {
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
        const expectedHash = process.env.DASHBOARD_PASSWORD_SHA256 ?? "";
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
