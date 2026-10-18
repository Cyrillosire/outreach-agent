"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { ProspectForm, type ProspectFormValues } from "@/components/prospect-form";
import { buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";

export function EditProspect({ prospectId, initial }: { prospectId: string; initial: ProspectFormValues }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return <button type="button" onClick={() => setOpen(true)} className={buttonClass("secondary")}>Edit</button>;
  }
  return (
    <div className="fixed inset-0 z-20 flex items-start justify-center overflow-y-auto bg-black/40 p-4" role="dialog"
         aria-modal="true" aria-labelledby="edit-title">
      <div className="mt-10 w-full max-w-3xl rounded-lg border border-border bg-surface p-5">
        <h2 id="edit-title" className="mb-4 text-base font-semibold">Edit prospect</h2>
        <ProspectForm prospectId={prospectId} initial={initial} onDone={() => setOpen(false)} />
      </div>
    </div>
  );
}

const REASONS = [
  { value: "manual", label: "Manual: don't contact" },
  { value: "unsubscribe", label: "Asked to unsubscribe" },
  { value: "complaint", label: "Complaint" },
  { value: "legal", label: "Legal or compliance hold" },
];

export function SuppressForm({ prospectId, hasEmail, domain }: { prospectId: string; hasEmail: boolean; domain: string | null }) {
  const router = useRouter();
  const [reason, setReason] = useState("manual");
  const [target, setTarget] = useState<"email" | "domain">(hasEmail ? "email" : "domain");
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const what = target === "email" ? "this email address" : `everyone at ${domain}`;
    if (!window.confirm(`Stop all outreach to ${what}? Scheduled emails will be cancelled.`)) return;
    setPending(true);
    setMessage(null);
    try {
      const r = await apiFetch<{ created: boolean; enrollments_stopped: number; actions_cancelled: number }>(
        `/prospects/${prospectId}/suppress`, { method: "POST", body: JSON.stringify({ reason, target, note: note || null }) },
      );
      setMessage({
        tone: "success",
        text: r.created
          ? `Suppressed. ${r.enrollments_stopped} campaign enrollment(s) stopped, ${r.actions_cancelled} scheduled action(s) cancelled.`
          : "Already suppressed for this reason; nothing changed.",
      });
      router.refresh();
    } catch (err) {
      setMessage({ tone: "danger", text: err instanceof ApiError ? err.message : "Couldn't suppress. Try again." });
    } finally {
      setPending(false);
    }
  }

  return (
    <Card title="Suppress outreach">
      <form onSubmit={onSubmit} className="space-y-3">
        <Field label="Reason" htmlFor="sup-reason">
          <select id="sup-reason" value={reason} onChange={(e) => setReason(e.target.value)} className={inputClass}>
            {REASONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </Field>
        <fieldset>
          <legend className="text-sm font-medium">Applies to</legend>
          <label className="mt-1 flex items-center gap-2 text-sm">
            <input type="radio" name="sup-target" checked={target === "email"} disabled={!hasEmail}
                   onChange={() => setTarget("email")} />
            This email address{hasEmail ? "" : " (no email on file)"}
          </label>
          <label className="mt-1 flex items-center gap-2 text-sm">
            <input type="radio" name="sup-target" checked={target === "domain"} disabled={!domain}
                   onChange={() => setTarget("domain")} />
            Everyone at {domain ?? "the company domain (unknown)"}
          </label>
        </fieldset>
        <Field label="Note (optional)" htmlFor="sup-note">
          <input id="sup-note" value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} className={inputClass} />
        </Field>
        {message ? <Notice tone={message.tone} title={message.text} /> : null}
        <button type="submit" disabled={pending || (!hasEmail && !domain)} className={buttonClass("danger")}>
          {pending ? "Suppressing…" : "Suppress"}
        </button>
        <p className="text-xs text-muted">Suppressions are permanent. Lifting one under a review policy arrives in a later milestone.</p>
      </form>
    </Card>
  );
}

export function DeleteProspectButton({ prospectId, name }: { prospectId: string; name: string }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  async function remove() {
    if (!window.confirm(`Permanently delete ${name}? Their research, drafts, campaign memberships and message history `
      + "are removed. Suppressions (opt-outs) are kept.")) return;
    setPending(true);
    try {
      await apiFetch(`/prospects/${prospectId}`, { method: "DELETE" });
      router.replace("/prospects");
      router.refresh();
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : "Delete failed.");
      setPending(false);
    }
  }
  return (
    <button type="button" onClick={remove} disabled={pending} className={buttonClass("danger")}>
      {pending ? "Deleting…" : "Delete"}
    </button>
  );
}
