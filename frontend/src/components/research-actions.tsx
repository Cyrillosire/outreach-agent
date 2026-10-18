"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { buttonClass } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import type { ResearchPhase } from "@/lib/types";

function phaseText(phase: ResearchPhase, position: number | null, companyName: string | null): string {
  switch (phase) {
    case "queued":
      return position ? `Queued: ${position} research job(s) ahead.` : "Queued: starting next.";
    case "researching":
      return "Researching this person now.";
    case "waiting_for_company":
      return `Person research done; finishing research on ${companyName ?? "their company"}.`;
    case "finishing":
      return "Saving results.";
    default:
      return "";
  }
}

/**
 * Start/refresh research for one prospect. While the queue is working on it (from job state, not the stored
 * status) the page refreshes itself, and stops as soon as the work is done.
 */
export function ResearchActions({ prospectId, phase, queuePosition, companyName, hasResearch }: {
  prospectId: string;
  phase: ResearchPhase;
  queuePosition: number | null;
  companyName: string | null;
  hasResearch: boolean;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inProgress = phase !== "idle";

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
    return (
      <span className="max-w-xs text-right text-xs text-muted" role="status">
        {phaseText(phase, queuePosition, companyName)} This page updates automatically.
      </span>
    );
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
