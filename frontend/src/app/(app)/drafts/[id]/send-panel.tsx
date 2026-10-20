"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Modal } from "@/components/modal";
import { Badge, buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { formatDate, OUTBOUND_LABELS } from "@/lib/labels";
import type { DraftDetail, Mailbox, OutboundMessage } from "@/lib/types";

const SETTLING = new Set(["queued", "sending"]);

/** Send an approved first email now, and show what happened to every send of this draft. */
export function SendPanel({ draft, mailboxes }: { draft: DraftDetail; mailboxes: Mailbox[] }) {
  const router = useRouter();
  const usable = mailboxes.filter((m) => m.status === "connected");
  const [mailboxId, setMailboxId] = useState(
    usable.find((m) => m.id === draft.sending_account_id)?.id ?? usable[0]?.id ?? "");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sends = draft.sends;
  const live = sends.find((s) => s.status && SETTLING.has(s.status));
  const done = sends.find((s) => s.status === "provider_accepted" || s.status === "unknown");
  const canSend = draft.status === "approved" && draft.is_current && draft.step_order === 0 && !live && !done;

  useEffect(() => {  // refresh while a send is in flight
    if (!live) return;
    const t = window.setInterval(() => router.refresh(), 2500);
    return () => window.clearInterval(t);
  }, [live, router]);

  async function send() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch<OutboundMessage>(`/drafts/${draft.id}/send`, {
        method: "POST", body: JSON.stringify({ email_account_id: mailboxId }),
      });
      setConfirming(false);
      router.refresh();
    } catch (err) {
      setError(errorMessage(err, "Couldn't send."));
    } finally {
      setBusy(false);
    }
  }

  if (!canSend && sends.length === 0) return null;
  const to = draft.prospect_name ?? "this contact";

  return (
    <Card title="Send">
      {error ? <div className="mb-3"><Notice tone="danger" title={error} /></div> : null}
      {canSend ? (
        usable.length === 0 ? (
          <Notice tone="warning" title="No connected mailbox">
            <Link href="/settings/mailboxes" className="underline">Connect a mailbox</Link> to send this email.
          </Notice>
        ) : (
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1">
              <Field label="Send from" htmlFor="send-from">
                <select id="send-from" className={inputClass} value={mailboxId} onChange={(e) => setMailboxId(e.target.value)}>
                  {usable.map((m) => <option key={m.id} value={m.id}>{m.email_address} ({m.sent_last_24h} sent today)</option>)}
                </select>
              </Field>
            </div>
            <button type="button" className={buttonClass("primary")} disabled={!mailboxId} onClick={() => setConfirming(true)}>
              Send now
            </button>
          </div>
        )
      ) : null}
      {sends.length ? (
        <ul className="mt-3 space-y-2 text-sm">
          {sends.map((s) => {
            const label = s.status ? OUTBOUND_LABELS[s.status] : null;
            return (
              <li key={s.id} className="rounded-md border border-border p-2">
                <div className="flex flex-wrap items-center gap-2">
                  {label ? <Badge tone={label.tone}>{label.label}</Badge> : null}
                  <span className="text-muted">{s.from_address} → {s.to_addresses.join(", ")}</span>
                  <span className="ml-auto text-xs text-muted">{formatDate(s.sent_at ?? s.created_at)}</span>
                </div>
                {s.last_error ? <p className="mt-1 text-xs text-danger">{s.last_error}</p>
                  : label?.hint ? <p className="mt-1 text-xs text-muted">{label.hint}</p> : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      <Modal open={confirming} title={`Send to ${to}?`} onClose={() => setConfirming(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirming(false)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy} onClick={send}>
                 {busy ? "Sending…" : "Send"}
               </button>
             </>}>
        <p className="text-sm">
          The approved version (v{draft.version}) goes out from{" "}
          <strong>{usable.find((m) => m.id === mailboxId)?.email_address}</strong> right away. Sending can&apos;t be undone.
        </p>
      </Modal>
    </Card>
  );
}
