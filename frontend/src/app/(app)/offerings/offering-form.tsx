"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ListInput } from "@/components/list-input";
import { Modal } from "@/components/modal";
import { buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { OFFERING_TYPE_LABELS, PROOF_KIND_LABELS } from "@/lib/labels";
import type { Offering, OfferingDetails, OfferingType, ProofKind, ProofPoint } from "@/lib/types";

const EMPTY_DETAILS: OfferingDetails = {
  problems_solved: [], capabilities: [], differentiators: [], proof_points: [], forbidden_claims: [],
  internal_notes: null,
};

/**
 * Create or edit an offering: what a campaign sells. Drafts may only describe the offering using the
 * capabilities and proof points written here, so precise, verifiable statements make better emails.
 */
export function OfferingForm({ offering }: { offering?: Offering }) {
  const router = useRouter();
  const [name, setName] = useState(offering?.name ?? "");
  const [type, setType] = useState<OfferingType>(offering?.offering_type ?? "product");
  const [category, setCategory] = useState(offering?.category ?? "");
  const [summary, setSummary] = useState(offering?.summary ?? "");
  const [details, setDetails] = useState<OfferingDetails>(offering?.details ?? EMPTY_DETAILS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const set = <K extends keyof OfferingDetails>(key: K, value: OfferingDetails[K]) => {
    setDetails({ ...details, [key]: value });
    setSaved(false);
  };
  const setProof = (i: number, patch: Partial<ProofPoint>) =>
    set("proof_points", details.proof_points.map((p, j) => (j === i ? { ...p, ...patch } : p)));

  async function save() {
    setBusy(true);
    setError(null);
    const body = JSON.stringify({
      name, offering_type: type, category: category.trim() || null, summary,
      details: { ...details, proof_points: details.proof_points.filter((p) => p.claim.trim()) },
    });
    try {
      const o = offering
        ? await apiFetch<Offering>(`/offerings/${offering.id}`, { method: "PATCH", body })
        : await apiFetch<Offering>("/offerings", { method: "POST", body });
      setSaved(true);
      if (!offering) router.replace(`/offerings/${o.id}`);
      router.refresh();
    } catch (err) {
      setError(errorMessage(err, "Couldn't save the offering."));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!offering) return;
    setBusy(true);
    try {
      const r = await apiFetch<{ outcome: string }>(`/offerings/${offering.id}`, { method: "DELETE" });
      if (r.outcome === "archived") {
        setConfirmDelete(false);
        setError("A campaign uses this offering, so it was archived instead of deleted.");
        router.refresh();
      } else {
        router.replace("/offerings");
        router.refresh();
      }
    } catch (err) {
      setError(errorMessage(err, "Delete failed."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error ? <Notice tone="danger" title={error} /> : null}
      {saved ? <Notice tone="success" title="Saved. New drafts will use this version." /> : null}

      <Card title="What is it?">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Name" htmlFor="o-name" hint="Internal name, e.g. “Clinical documentation (ambient scribe)”.">
            <input id="o-name" className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Type" htmlFor="o-type">
            <select id="o-type" className={inputClass} value={type} onChange={(e) => setType(e.target.value as OfferingType)}>
              {(Object.keys(OFFERING_TYPE_LABELS) as OfferingType[]).map((t) => (
                <option key={t} value={t}>{OFFERING_TYPE_LABELS[t]}</option>
              ))}
            </select>
          </Field>
          <Field label="Category" htmlFor="o-cat" hint="e.g. clinical documentation, revenue-cycle services, research partnership.">
            <input id="o-cat" className={inputClass} value={category} onChange={(e) => setCategory(e.target.value)} />
          </Field>
          <div className="md:col-span-2">
            <Field label="Summary" htmlFor="o-summary"
                   hint="Two or three plain sentences: what it is, who it's for, and the outcome it delivers.">
              <textarea id="o-summary" rows={3} className={inputClass} value={summary}
                        onChange={(e) => setSummary(e.target.value)} />
            </Field>
          </div>
        </div>
      </Card>

      <Card title="Why would someone want it?">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Problems it solves" htmlFor="o-problems" hint="One per line, in the buyer's words.">
            <ListInput id="o-problems" value={details.problems_solved} onChange={(v) => set("problems_solved", v)} />
          </Field>
          <Field label="Capabilities" htmlFor="o-caps" hint="One per line. Drafts may only describe what's listed here.">
            <ListInput id="o-caps" value={details.capabilities} onChange={(v) => set("capabilities", v)} />
          </Field>
          <Field label="Differentiators" htmlFor="o-diff" hint="What makes it different from alternatives. One per line.">
            <ListInput id="o-diff" value={details.differentiators} onChange={(v) => set("differentiators", v)} />
          </Field>
          <Field label="Never claim" htmlFor="o-forbidden"
                 hint="Phrases or promises a draft must never make (e.g. “replaces doctors”, “HIPAA certified”).">
            <ListInput id="o-forbidden" value={details.forbidden_claims} onChange={(v) => set("forbidden_claims", v)} />
          </Field>
        </div>
      </Card>

      <Card title="Proof points">
        <p className="mb-3 text-sm text-muted">
          The only numbers, results, and customer names a draft may use. Any figure in a draft that isn&apos;t
          here or in the prospect&apos;s research is flagged and blocks approval until you confirm it.
        </p>
        <div className="space-y-3">
          {details.proof_points.map((p, i) => (
            <div key={p.id ?? `new-${i}`} className="grid gap-2 rounded-md border border-border p-3 md:grid-cols-[1fr_12rem_auto]">
              <input aria-label="Proof point claim" className={inputClass} value={p.claim}
                     placeholder="e.g. Cut documentation time by 40% in a 3-hospital pilot (2025)"
                     onChange={(e) => setProof(i, { claim: e.target.value })} />
              <select aria-label="Proof point kind" className={inputClass} value={p.kind}
                      onChange={(e) => setProof(i, { kind: e.target.value as ProofKind })}>
                {(Object.keys(PROOF_KIND_LABELS) as ProofKind[]).map((k) => (
                  <option key={k} value={k}>{PROOF_KIND_LABELS[k]}</option>
                ))}
              </select>
              <button type="button" className={buttonClass("ghost")}
                      onClick={() => set("proof_points", details.proof_points.filter((_, j) => j !== i))}>Remove</button>
              <input aria-label="Where this is documented" className={`${inputClass} md:col-span-2`}
                     placeholder="Where it's documented (internal, never sent)" value={p.source ?? ""}
                     onChange={(e) => setProof(i, { source: e.target.value || null })} />
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={p.can_name_publicly}
                       onChange={(e) => setProof(i, { can_name_publicly: e.target.checked })} />
                OK to mention
              </label>
            </div>
          ))}
          <button type="button" className={buttonClass()}
                  onClick={() => set("proof_points", [...details.proof_points,
                    { claim: "", kind: "metric", source: null, can_name_publicly: true }])}>
            Add proof point
          </button>
        </div>
      </Card>

      <Card title="Internal notes">
        <Field label="Notes for your team" htmlFor="o-notes" hint="Never shown to prospects or sent to the writing model.">
          <textarea id="o-notes" rows={2} className={inputClass} value={details.internal_notes ?? ""}
                    onChange={(e) => set("internal_notes", e.target.value || null)} />
        </Field>
      </Card>

      <div className="flex flex-wrap gap-2">
        <button type="button" className={buttonClass("primary")} disabled={busy} onClick={save}>
          {busy ? "Saving…" : offering ? "Save changes" : "Create offering"}
        </button>
        {offering ? (
          <button type="button" className={buttonClass("danger")} onClick={() => setConfirmDelete(true)}>Delete</button>
        ) : null}
      </div>

      <Modal open={confirmDelete} title="Delete this offering?" onClose={() => setConfirmDelete(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirmDelete(false)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy} onClick={remove}>Delete</button>
             </>}>
        <p className="text-sm">If a campaign uses it, it&apos;s archived instead so existing drafts keep their context.</p>
      </Modal>
    </div>
  );
}
