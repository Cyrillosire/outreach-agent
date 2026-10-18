"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { buttonClass, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import type { Prospect, RowIssue } from "@/lib/types";

const FIELDS = [
  { name: "full_name", label: "Full name" },
  { name: "email", label: "Work email", type: "email" },
  { name: "first_name", label: "First name" },
  { name: "last_name", label: "Last name" },
  { name: "title", label: "Job title" },
  { name: "linkedin_url", label: "LinkedIn profile URL", hint: "Personal profile (linkedin.com/in/…)" },
  { name: "company_name", label: "Company name" },
  { name: "company_domain", label: "Company domain", hint: "e.g. example.com. Used to share company research." },
  { name: "company_website", label: "Company website" },
  { name: "industry", label: "Industry" },
  { name: "country", label: "Country / region" },
  { name: "location", label: "Location" },
] as const;

type FieldName = (typeof FIELDS)[number]["name"];
export type ProspectFormValues = Partial<Record<FieldName, string>>;

interface WriteResult {
  prospect: Prospect;
  warnings: RowIssue[];
}

/** Create (no prospectId) or edit (sends only changed fields) a prospect, with server-side validation. */
export function ProspectForm({ prospectId, initial = {}, onDone }: {
  prospectId?: string;
  initial?: ProspectFormValues;
  onDone?: () => void;
}) {
  const router = useRouter();
  const [values, setValues] = useState<ProspectFormValues>(initial);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<{ message: string; issues: RowIssue[]; existingId?: string } | null>(null);
  const [warnings, setWarnings] = useState<RowIssue[]>([]);
  const idempotencyKey = useRef(typeof crypto !== "undefined" ? crypto.randomUUID() : "");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    setWarnings([]);
    try {
      let result: WriteResult;
      if (prospectId) {
        const changes: Record<string, string | null> = {};
        for (const f of FIELDS) {
          const next = (values[f.name] ?? "").trim();
          if (next !== (initial[f.name] ?? "").trim()) changes[f.name] = next || null;
        }
        // Company fields are resolved together on the server.
        if (["company_name", "company_domain", "company_website"].some((k) => k in changes)) {
          for (const k of ["company_name", "company_domain", "company_website"] as const) {
            changes[k] = (values[k] ?? "").trim() || null;
          }
        }
        if (Object.keys(changes).length === 0) {
          onDone?.();
          return;
        }
        result = await apiFetch<WriteResult>(`/prospects/${prospectId}`, { method: "PATCH", body: JSON.stringify(changes) });
      } else {
        const body = Object.fromEntries(FIELDS.map((f) => [f.name, (values[f.name] ?? "").trim() || null]));
        result = await apiFetch<WriteResult>("/prospects", {
          method: "POST", body: JSON.stringify(body), headers: { "Idempotency-Key": idempotencyKey.current },
        });
      }
      if (result.warnings.length) setWarnings(result.warnings);
      router.refresh();
      if (prospectId) onDone?.();
      else router.push(`/prospects/${result.prospect.id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        const details = err.details as RowIssue[] | { prospect_id?: string } | undefined;
        setError({
          message: err.message,
          issues: Array.isArray(details) ? details : [],
          existingId: !Array.isArray(details) ? details?.prospect_id : undefined,
        });
      } else {
        setError({ message: "Couldn't save. Check your connection and try again.", issues: [] });
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate>
      <div className="grid gap-4 sm:grid-cols-2">
        {FIELDS.map((f) => (
          <Field key={f.name} label={f.label} htmlFor={`pf-${f.name}`} hint={"hint" in f ? f.hint : undefined}>
            <input
              id={`pf-${f.name}`} name={f.name} type={"type" in f ? f.type : "text"}
              value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
              className={inputClass}
            />
          </Field>
        ))}
      </div>
      {error ? (
        <Notice tone="danger" title={error.message}>
          {error.issues.length ? (
            <ul className="list-disc pl-5">{error.issues.map((i, n) => <li key={n}>{i.message}</li>)}</ul>
          ) : null}
          {error.existingId ? (
            <Link className="underline" href={`/prospects/${error.existingId}`}>Open the existing prospect</Link>
          ) : null}
        </Notice>
      ) : null}
      {warnings.length ? (
        <Notice tone="warning" title="Saved with warnings">
          <ul className="list-disc pl-5">{warnings.map((w, n) => <li key={n}>{w.message}</li>)}</ul>
        </Notice>
      ) : null}
      <div className="flex gap-2">
        <button type="submit" disabled={pending} className={buttonClass("primary")}>
          {pending ? "Saving…" : prospectId ? "Save changes" : "Create prospect"}
        </button>
        {onDone ? <button type="button" onClick={onDone} className={buttonClass("secondary")}>Cancel</button> : null}
      </div>
    </form>
  );
}
