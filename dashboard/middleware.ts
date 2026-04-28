// Force authentication on every page except /login and /api/auth/*.
// API proxy routes are also gated, redundantly, in their handlers.

export { default } from "next-auth/middleware";

export const config = {
  matcher: [
    // Match everything except: NextAuth itself, the login page, static
    // assets, and Next's internal routes.
    "/((?!api/auth|login|_next/static|_next/image|favicon.ico).*)",
  ],
};
