"use client";

import { signIn } from "next-auth/react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, type FormEvent, useState } from "react";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const search = useSearchParams();
  const callbackUrl = search?.get("callbackUrl") ?? "/";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    const result = await signIn("credentials", {
      redirect: false,
      password,
      callbackUrl,
    });
    setLoading(false);
    if (result?.ok) {
      router.push(callbackUrl);
    } else {
      setError("Wrong password.");
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center px-4">
      <form
        onSubmit={onSubmit}
        className="card-raised w-full max-w-sm p-6 space-y-4"
        aria-label="login"
      >
        <header className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">HTA</h1>
          <p className="text-text1 text-sm">Operator dashboard. Read-only.</p>
        </header>
        <label className="block space-y-1">
          <span className="label">Password</span>
          <input
            autoFocus
            autoComplete="current-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="num w-full bg-bg0 border border-border rounded px-3 py-2 outline-none focus:border-accent"
            placeholder="••••••••"
          />
        </label>
        {error && (
          <p className="text-bear text-sm" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={loading || !password}
          className="w-full bg-accent text-bg0 font-medium py-2 rounded hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? "checking…" : "sign in"}
        </button>
      </form>
    </main>
  );
}
