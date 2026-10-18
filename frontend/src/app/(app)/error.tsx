"use client";

import { Notice } from "@/components/ui";

export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="max-w-xl space-y-4">
      <Notice tone="danger" title="This page could not be loaded">
        {error.message || "An unexpected error occurred."}
        {error.digest ? <span className="block text-xs opacity-75">Reference: {error.digest}</span> : null}
      </Notice>
      <button
        type="button" onClick={reset}
        className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-muted"
      >
        Try again
      </button>
    </div>
  );
}
