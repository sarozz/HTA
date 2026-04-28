// Force authentication on every page except /login and /api/auth/*.
// API proxy routes are also gated, redundantly, in their handlers.

import { withAuth } from "next-auth/middleware";

// Edge runtime can't import lib/auth.ts (which uses node:crypto), so we
// duplicate the secret-resolution fallback here. Keep this in sync with
// FALLBACK_SECRET in lib/auth.ts.
const FALLBACK_SECRET =
  "hta-static-fallback-set-NEXTAUTH_SECRET-in-vercel-env-asap-please-do-it";

const RESOLVED_SECRET =
  (process.env.NEXTAUTH_SECRET && process.env.NEXTAUTH_SECRET.trim()) ||
  (process.env.AUTH_SECRET && process.env.AUTH_SECRET.trim()) ||
  FALLBACK_SECRET;

export default withAuth({
  secret: RESOLVED_SECRET,
});

export const config = {
  matcher: [
    // Match everything except: NextAuth itself, the login page, static
    // assets, and Next's internal routes.
    "/((?!api/auth|login|_next/static|_next/image|favicon.ico).*)",
  ],
};
