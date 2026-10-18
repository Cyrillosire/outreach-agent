"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { buttonClass } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";

/** Start/refresh research for one prospect, and keep the page fresh while research is running. */
export function ResearchActions({ prospectId, inProgress, hasResearch }: {
  prospectId: string;
  inProgress: boolean;
  hasResearch: boolean;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!inProgress) return;
    const timer = window.setInterval(() => router.refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [inProgress, router]);

  async function start(force: boolean) {
    setPending(true);
    setError(null);
    try {
      await apiFetch(`/prospects/${prospectId}/research`, { method: "POST", body: JSON.stringify({ force }) });
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't start research. Try again.");
    } finally {
      setPending(false);
    }
  }

  if (inProgress) {
    return <span className="text-xs text-muted">Researching… this page refreshes automatically.</span>;
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <button type="button" disabled={pending} onClick={() => start(hasResearch)} className={buttonClass("secondary")}>
        {pending ? "Starting…" : hasResearch ? "Refresh research" : "Start research"}
      </button>
      {error ? <span className="text-xs text-danger">{error}</span> : null}
    </div>
  );
}

export function RetryFailedButton({ count }: { count: number }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  if (count === 0) return null;

  async function retry() {
    setPending(true);
    try {
      const r = await apiFetch<{ requeued: number }>("/research/retry-failed", { method: "POST" });
      setMessage(`Re-queued ${r.requeued} prospect(s).`);
      router.refresh();
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "Retry failed.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button type="button" disabled={pending} onClick={retry} className={buttonClass("primary")}>
        {pending ? "Re-queuing…" : `Retry ${count} failed`}
      </button>
      {message ? <span className="text-xs text-muted">{message}</span> : null}
    </div>
  );
}
