"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, type ChangeEvent, type FormEvent } from "react";

import { buttonClass } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import type { ImportDetail } from "@/lib/types";

export function UploadForm() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  // One key per selected file: a double click or network retry replays instead of creating a second import.
  const idempotencyKey = useRef<string>("");

  function onChange(event: ChangeEvent<HTMLInputElement>) {
    setFile(event.target.files?.[0] ?? null);
    setError(null);
    idempotencyKey.current = crypto.randomUUID();
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setPending(true);
    setError(null);
    const body = new FormData();
    body.append("file", file);
    try {
      const imp = await apiFetch<ImportDetail>("/imports", {
        method: "POST", body, headers: { "Idempotency-Key": idempotencyKey.current },
      });
      router.push(`/prospects/imports/${imp.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed. Check your connection and try again.");
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <label
        htmlFor="csv"
        className="flex cursor-pointer flex-col items-center justify-center rounded-md border border-dashed border-border px-4 py-8 text-center hover:bg-surface-muted"
      >
        <span className="text-sm font-medium">{file ? file.name : "Choose a CSV file"}</span>
        <span className="mt-1 text-xs text-muted">
          {file ? `${(file.size / 1024).toFixed(1)} KB` : "Up to 5 MB and 5,000 rows"}
        </span>
        <input id="csv" type="file" accept=".csv,.tsv,.txt,text/csv" className="sr-only" onChange={onChange} />
      </label>
      {error ? <p role="alert" className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">{error}</p> : null}
      <button type="submit" disabled={!file || pending} className={`${buttonClass("primary")} w-full`}>
        {pending ? "Uploading…" : "Upload and review"}
      </button>
    </form>
  );
}
