"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import { CTA_LABELS, OFFERING_TYPE_LABELS } from "@/lib/labels";
import type { Campaign, Offering, OutreachMotion, Playbook } from "@/lib/types";

export function NewCampaignForm({ playbooks, offerings }: { playbooks: Playbook[]; offerings: Offering[] }) {
  const router = useRouter();
  const [motion, setMotion] = useState<OutreachMotion>(playbooks[0]?.motion ?? "product_sale");
  const [offeringId, setOfferingId] = useState(offerings[0]?.id ?? "");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const c = await apiFetch<Campaign>("/campaigns", {
        method: "POST", body: JSON.stringify({ name, motion, offering_id: offeringId || null }),
      });
      router.push(`/campaigns/${c.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't create the campaign.");
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {error ? <Notice tone="danger" title={error} /> : null}
      <Card title="1. What kind of outreach is this?">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" role="radiogroup" aria-label="Playbook">
          {playbooks.map((p) => (
            <button
              key={p.motion} type="button" role="radio" aria-checked={motion === p.motion}
              onClick={() => setMotion(p.motion)}
              className={`rounded-lg border p-4 text-left transition-colors ${motion === p.motion
                ? "border-accent bg-info-bg/40 ring-2 ring-accent/30" : "border-border hover:bg-surface-muted"}`}
            >
              <div className="font-medium">{p.label}</div>
              <p className="mt-1 text-sm text-muted">{p.description}</p>
              <div className="mt-3 text-xs text-muted">
                Asks for: {CTA_LABELS[p.default_brief.cta.type].toLowerCase()} · follow-ups on day{" "}
                {p.followup_days.slice(1).join(", ") || "—"}
              </div>
            </button>
          ))}
        </div>
      </Card>

      <Card title="2. What are you offering?">
        {offerings.length === 0 ? (
          <Notice tone="warning" title="No offerings yet">
            <Link href="/offerings/new" className="underline">Define an offering</Link> first (what you&apos;re selling
            or proposing), or create the campaign now and attach one later.
          </Notice>
        ) : (
          <Field label="Offering" htmlFor="c-offering"
                 hint="Drafts describe only this offering, using its capabilities and proof points.">
            <select id="c-offering" className={inputClass} value={offeringId} onChange={(e) => setOfferingId(e.target.value)}>
              {offerings.map((o) => (
                <option key={o.id} value={o.id}>{o.name} ({OFFERING_TYPE_LABELS[o.offering_type]})</option>
              ))}
              <option value="">Decide later</option>
            </select>
          </Field>
        )}
      </Card>

      <Card title="3. Name it">
        <Field label="Campaign name" htmlFor="c-name" hint="e.g. “Delhi NCR hospitals: clinical documentation, Q4”.">
          <input id="c-name" className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </Card>

      <button type="button" className={buttonClass("primary")} disabled={busy || name.trim().length < 2} onClick={create}>
        {busy ? "Creating…" : "Create and edit the brief"}
      </button>
    </div>
  );
}
