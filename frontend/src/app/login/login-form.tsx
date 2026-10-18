"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";

function describe(err: unknown): string {
  if (!(err instanceof ApiError)) return "Could not reach the server. Check your connection and try again.";
  switch (err.code) {
    case "invalid_credentials":
      return "Invalid email or password.";
    case "rate_limited": {
      const seconds = (err.details as { retry_after_seconds?: number } | undefined)?.retry_after_seconds;
      return `Too many attempts. Try again in ${seconds ? Math.ceil(seconds / 60) : "a few"} minute(s).`;
    }
    case "validation_error":
      return "Enter a valid email address and password.";
    case "backend_unavailable":
    case "service_unavailable":
      return "Sign-in is temporarily unavailable. Try again shortly.";
    default:
      return err.message;
  }
}

export function LoginForm({ next }: { next: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError(null);
    try {
      await apiFetch("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      router.replace(next);
      router.refresh();
    } catch (err) {
      setError(describe(err));
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
      <div>
        <label htmlFor="email" className="block text-sm font-medium">Email</label>
        <input
          id="email" name="email" type="email" autoComplete="username" required
          className="mt-1 block w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/30"
        />
      </div>
      <div>
        <label htmlFor="password" className="block text-sm font-medium">Password</label>
        <input
          id="password" name="password" type="password" autoComplete="current-password" required
          className="mt-1 block w-full rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent focus:ring-2 focus:ring-accent/30"
        />
      </div>
      {error ? <p role="alert" className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p> : null}
      <button
        type="submit" disabled={pending}
        className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-foreground hover:opacity-90 disabled:opacity-60"
      >
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
