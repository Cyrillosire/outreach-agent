"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";

export function LogoutButton() {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function logout() {
    setPending(true);
    setError(null);
    try {
      await apiFetch<void>("/auth/logout", { method: "POST" });
    } catch (err) {
      // An already-expired session is effectively logged out; anything else is surfaced.
      if (!(err instanceof ApiError && err.status === 401)) {
        setError(err instanceof Error ? err.message : "Sign out failed");
        setPending(false);
        return;
      }
    }
    router.replace("/login");
    router.refresh();
  }

  return (
    <div>
      <button
        type="button"
        onClick={logout}
        disabled={pending}
        className="w-full rounded-md px-3 py-2 text-left text-sm text-muted hover:bg-surface-muted disabled:opacity-60"
      >
        {pending ? "Signing out…" : "Sign out"}
      </button>
      {error ? <p className="px-3 text-xs text-danger">{error}</p> : null}
    </div>
  );
}
