"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ListInput } from "@/components/list-input";
import { buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { CTA_LABELS, MOTION_LABELS } from "@/lib/labels";
import type { Campaign, CampaignBrief, CtaType, Offering, OutreachMotion, Persona, Playbook } from "@/lib/types";

const EMPTY_PERSONA: Persona = { name: "", title_keywords: [], pains: [], priorities: [], angle: null };

/**
 * The campaign brief: everything the writer needs besides the prospect's research. Saving bumps the
 * brief version; drafts already written keep the version they were written from.
 */
export function BriefEditor({ campaign, offerings, playbooks }: {
  campaign: Campaign;
  offerings: Offering[];
  playbooks: Playbook[];
}) {
  const router = useRouter();
  const [name, setName] = useState(campaign.name);
  const [motion, setMotion] = useState<OutreachMotion>(campaign.motion);
  const [offeringId, setOfferingId] = useState(campaign.offering_id ?? "");
  const [brief, setBrief] = useState<CampaignBrief>(campaign.brief);
  const [days, setDays] = useState(campaign.step_days.join(", "));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [dirty, setDirty] = useState(false);

  const playbook = playbooks.find((p) => p.motion === motion);
  const offering = offerings.find((o) => o.id === offeringId);
  const blockers = campaign.readiness.filter((r) => r.severity === "blocker");
  const recommended = campaign.readiness.filter((r) => r.severity === "recommended");

  function update<K extends keyof CampaignBrief>(key: K, value: CampaignBrief[K]) {
    setBrief((b) => ({ ...b, [key]: value }));
    setDirty(true);
  }
  function nested<K extends "audience" | "cta" | "style" | "sender" | "sequence">(
    key: K, patch: Partial<CampaignBrief[K]>,
  ) {
    setBrief((b) => ({ ...b, [key]: { ...b[key], ...patch } }));
    setDirty(true);
  }
  const personas = brief.audience.personas;
  const setPersona = (i: number, patch: Partial<Persona>) =>
    nested("audience", { personas: personas.map((p, j) => (j === i ? { ...p, ...patch } : p)) });

  async function save() {
    setBusy(true);
    setNotice(null);
    try {
      await apiFetch(`/campaigns/${campaign.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name, motion, offering_id: offeringId || null,
          brief: { ...brief, cta: { ...brief.cta, link: brief.cta.link || null } },
        }),
      });
      setDirty(false);
      setNotice({ tone: "success", text: "Brief saved. New drafts use this version." });
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't save the brief.") });
    } finally {
      setBusy(false);
    }
  }

  async function saveSteps() {
    const parsed = days.split(/[,\s]+/).filter(Boolean).map(Number);
    if (parsed.some((d) => !Number.isInteger(d) || d < 0)) {
      setNotice({ tone: "danger", text: "Days must be whole numbers, e.g. 0, 3, 7, 14." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await apiFetch(`/campaigns/${campaign.id}/steps`, { method: "PUT", body: JSON.stringify({ days: parsed }) });
      setNotice({ tone: "success", text: "Follow-up schedule saved." });
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't save the schedule.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_20rem]">
      <div className="space-y-4">
        {notice ? <Notice tone={notice.tone} title={notice.text} /> : null}

        <Card title="What are we selling, and how?">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Campaign name" htmlFor="b-name">
              <input id="b-name" className={inputClass} value={name}
                     onChange={(e) => { setName(e.target.value); setDirty(true); }} />
            </Field>
            <Field label="Offering" htmlFor="b-offering" hint="What this campaign sells or proposes.">
              <select id="b-offering" className={inputClass} value={offeringId}
                      onChange={(e) => { setOfferingId(e.target.value); setDirty(true); }}>
                <option value="">Not chosen</option>
                {offerings.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
              </select>
            </Field>
            <Field label="Approach" htmlFor="b-motion" hint={playbook?.description}>
              <select id="b-motion" className={inputClass} value={motion}
                      onChange={(e) => { setMotion(e.target.value as OutreachMotion); setDirty(true); }}>
                {playbooks.map((p) => <option key={p.motion} value={p.motion}>{MOTION_LABELS[p.motion]}</option>)}
              </select>
            </Field>
            <div className="text-sm">
              {offering ? (
                <div className="rounded-md bg-surface-muted p-3">
                  <div className="font-medium">{offering.name}</div>
                  <p className="mt-1 text-muted">{offering.summary}</p>
                  <p className="mt-2 text-xs text-muted">
                    {offering.details.capabilities.length} capabilities · {offering.details.proof_points.length} proof
                    points · <Link href={`/offerings/${offering.id}`} className="underline">edit offering</Link>
                  </p>
                </div>
              ) : (
                <Link href="/offerings/new" className="underline">Define a new offering</Link>
              )}
            </div>
          </div>
          {playbook ? (
            <details className="mt-4 text-sm">
              <summary className="cursor-pointer text-muted">How the writer approaches a {playbook.label.toLowerCase()}</summary>
              <p className="mt-2 whitespace-pre-line text-muted">{playbook.guidance}</p>
            </details>
          ) : null}
        </Card>

        <Card title="Goal and timing">
          <div className="grid gap-4">
            <Field label="Goal" htmlFor="b-goal" hint="What a successful reply looks like, e.g. “Book discovery calls with hospital digital leaders”.">
              <input id="b-goal" className={inputClass} value={brief.goal} onChange={(e) => update("goal", e.target.value)} />
            </Field>
            <Field label="Why now (optional)" htmlFor="b-why"
                   hint="A true reason that applies to everyone in this campaign: a regulation, deadline, or event. Leave empty if there isn't one.">
              <input id="b-why" className={inputClass} value={brief.why_now ?? ""}
                     onChange={(e) => update("why_now", e.target.value || null)} />
            </Field>
          </div>
        </Card>

        <Card title="Who are we writing to?">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Industries" htmlFor="b-ind" hint="One per line.">
              <ListInput id="b-ind" value={brief.audience.industries} onChange={(v) => nested("audience", { industries: v })} />
            </Field>
            <Field label="Organization types" htmlFor="b-org" hint="e.g. multi-specialty hospitals.">
              <ListInput id="b-org" value={brief.audience.organization_types}
                         onChange={(v) => nested("audience", { organization_types: v })} />
            </Field>
            <Field label="Regions" htmlFor="b-reg">
              <ListInput id="b-reg" value={brief.audience.regions} onChange={(v) => nested("audience", { regions: v })} />
            </Field>
          </div>

          <h3 className="mb-2 mt-5 text-sm font-semibold">Personas</h3>
          <p className="mb-3 text-sm text-muted">
            Contacts are matched to a persona by job-title keywords; the writer frames the offering around that
            persona&apos;s pains and priorities.
          </p>
          <div className="space-y-3">
            {personas.map((p, i) => (
              <div key={i} className="rounded-md border border-border p-3">
                <div className="grid gap-3 md:grid-cols-2">
                  <Field label="Persona" htmlFor={`p-name-${i}`}>
                    <input id={`p-name-${i}`} className={inputClass} value={p.name} placeholder="e.g. Hospital CIO / IT head"
                           onChange={(e) => setPersona(i, { name: e.target.value })} />
                  </Field>
                  <Field label="Job-title keywords" htmlFor={`p-kw-${i}`} hint="One per line, e.g. CIO, digital, IT head.">
                    <ListInput id={`p-kw-${i}`} rows={2} value={p.title_keywords}
                               onChange={(v) => setPersona(i, { title_keywords: v })} />
                  </Field>
                  <Field label="Their pains" htmlFor={`p-pains-${i}`} hint="As you understand them. Written as questions, never asserted.">
                    <ListInput id={`p-pains-${i}`} rows={2} value={p.pains} onChange={(v) => setPersona(i, { pains: v })} />
                  </Field>
                  <Field label="What they're measured on" htmlFor={`p-pri-${i}`}>
                    <ListInput id={`p-pri-${i}`} rows={2} value={p.priorities} onChange={(v) => setPersona(i, { priorities: v })} />
                  </Field>
                  <div className="md:col-span-2">
                    <Field label="Angle (optional)" htmlFor={`p-angle-${i}`} hint="How to frame the offering for this role.">
                      <input id={`p-angle-${i}`} className={inputClass} value={p.angle ?? ""}
                             onChange={(e) => setPersona(i, { angle: e.target.value || null })} />
                    </Field>
                  </div>
                </div>
                <button type="button" className={`${buttonClass("ghost")} mt-2`}
                        onClick={() => nested("audience", { personas: personas.filter((_, j) => j !== i) })}>
                  Remove persona
                </button>
              </div>
            ))}
            <button type="button" className={buttonClass()}
                    onClick={() => nested("audience", { personas: [...personas, { ...EMPTY_PERSONA }] })}>
              Add persona
            </button>
          </div>
        </Card>

        <Card title="Who is a good fit?">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Fit signals" htmlFor="b-fit" hint="Research findings that make a contact relevant, e.g. “HIS rollout”, “new hospital opening”.">
              <ListInput id="b-fit" rows={4} value={brief.fit_signals} onChange={(v) => update("fit_signals", v)} />
            </Field>
            <Field label="Disqualifiers" htmlFor="b-dq" hint="Don't reach out if research shows these, e.g. “already uses a competing scribe”.">
              <ListInput id="b-dq" rows={4} value={brief.disqualifiers} onChange={(v) => update("disqualifiers", v)} />
            </Field>
            <Field label="Emphasize" htmlFor="b-vf" hint="Which offering problems or capabilities to lead with in this campaign.">
              <ListInput id="b-vf" rows={4} value={brief.value_focus} onChange={(v) => update("value_focus", v)} />
            </Field>
          </div>
          <label className="mt-4 flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5" checked={brief.allow_company_only_personalization}
                   onChange={(e) => update("allow_company_only_personalization", e.target.checked)} />
            <span>
              Also draft for contacts whose role couldn&apos;t be confirmed by research, using company evidence only.
              <span className="block text-muted">Off by default: such drafts are less personal and riskier.</span>
            </span>
          </label>
        </Card>

        <Card title="What are we asking for?">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Call to action" htmlFor="b-cta">
              <select id="b-cta" className={inputClass} value={brief.cta.type}
                      onChange={(e) => nested("cta", { type: e.target.value as CtaType })}>
                {(Object.keys(CTA_LABELS) as CtaType[]).map((t) => <option key={t} value={t}>{CTA_LABELS[t]}</option>)}
              </select>
            </Field>
            <Field label="Specifics" htmlFor="b-ctad" hint="e.g. “a 20-minute call next week”.">
              <input id="b-ctad" className={inputClass} value={brief.cta.details}
                     onChange={(e) => nested("cta", { details: e.target.value })} />
            </Field>
            <Field label="Link (optional)" htmlFor="b-ctal" hint="Scheduling or RSVP link; the only link allowed in drafts.">
              <input id="b-ctal" type="url" className={inputClass} value={brief.cta.link ?? ""}
                     onChange={(e) => nested("cta", { link: e.target.value || null })} />
            </Field>
          </div>
        </Card>

        <Card title="Voice and length">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Tone" htmlFor="b-tone">
              <select id="b-tone" className={inputClass} value={brief.style.tone}
                      onChange={(e) => nested("style", { tone: e.target.value as CampaignBrief["style"]["tone"] })}>
                <option value="formal">Formal</option>
                <option value="professional">Professional</option>
                <option value="warm">Warm</option>
                <option value="direct">Direct</option>
              </select>
            </Field>
            <Field label="Maximum words" htmlFor="b-words" hint="First email; follow-ups are shorter.">
              <input id="b-words" type="number" min={50} max={250} className={inputClass} value={brief.style.max_words}
                     onChange={(e) => nested("style", { max_words: Number(e.target.value) })} />
            </Field>
            <Field label="Language" htmlFor="b-lang">
              <input id="b-lang" className={inputClass} value={brief.style.language}
                     onChange={(e) => nested("style", { language: e.target.value })} />
            </Field>
            <Field label="Greeting" htmlFor="b-greet" hint="{first_name} is filled in; titles like Dr. are respected.">
              <input id="b-greet" className={inputClass} value={brief.style.greeting}
                     onChange={(e) => nested("style", { greeting: e.target.value })} />
            </Field>
            <Field label="Sign-off" htmlFor="b-signoff">
              <input id="b-signoff" className={inputClass} value={brief.style.sign_off}
                     onChange={(e) => nested("style", { sign_off: e.target.value })} />
            </Field>
            <Field label="Banned phrases" htmlFor="b-banned" hint="One per line.">
              <ListInput id="b-banned" rows={2} value={brief.style.banned_phrases}
                         onChange={(v) => nested("style", { banned_phrases: v })} />
            </Field>
          </div>
        </Card>

        <Card title="Sender">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Name" htmlFor="s-name">
              <input id="s-name" className={inputClass} value={brief.sender.name}
                     onChange={(e) => nested("sender", { name: e.target.value })} />
            </Field>
            <Field label="Title" htmlFor="s-title">
              <input id="s-title" className={inputClass} value={brief.sender.title}
                     onChange={(e) => nested("sender", { title: e.target.value })} />
            </Field>
            <Field label="Company" htmlFor="s-company">
              <input id="s-company" className={inputClass} value={brief.sender.company}
                     onChange={(e) => nested("sender", { company: e.target.value })} />
            </Field>
            <div className="md:col-span-3 grid gap-4 md:grid-cols-2">
              <Field label="Signature" htmlFor="s-sig" hint="Lines under the sign-off. Added exactly as written.">
                <textarea id="s-sig" rows={3} className={inputClass} value={brief.sender.signature}
                          onChange={(e) => nested("sender", { signature: e.target.value })} />
              </Field>
              <Field label="Compliance footer" htmlFor="s-footer"
                     hint="Required. Your company's postal address and how to opt out (e.g. “Reply 'unsubscribe' to opt out.”).">
                <textarea id="s-footer" rows={3} className={inputClass} value={brief.sender.compliance_footer}
                          onChange={(e) => nested("sender", { compliance_footer: e.target.value })} />
              </Field>
            </div>
          </div>
        </Card>

        <Card title="Follow-ups">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Send on days" htmlFor="b-days"
                   hint="Days after the first email, starting with 0. e.g. 0, 3, 7, 14 is the first email plus three follow-ups.">
              <div className="flex gap-2">
                <input id="b-days" className={inputClass} value={days} onChange={(e) => setDays(e.target.value)} />
                <button type="button" className={buttonClass()} disabled={busy} onClick={saveSteps}>Save schedule</button>
              </div>
            </Field>
            <Field label="When to draft follow-ups" htmlFor="b-fu">
              <select id="b-fu" className={inputClass} value={brief.sequence.draft_followups}
                      onChange={(e) => nested("sequence", {
                        draft_followups: e.target.value as CampaignBrief["sequence"]["draft_followups"],
                      })}>
                <option value="after_initial_approved">After the first email is approved (recommended)</option>
                <option value="upfront">Together with the first email</option>
                <option value="manual">Only when I ask</option>
              </select>
            </Field>
          </div>
        </Card>

        <div className="sticky bottom-2 flex items-center gap-3 rounded-lg border border-border bg-surface p-3 shadow-sm">
          <button type="button" className={buttonClass("primary")} disabled={busy || !dirty} onClick={save}>
            {busy ? "Saving…" : "Save brief"}
          </button>
          <span className="text-sm text-muted">
            {dirty ? "Unsaved changes" : `Version ${campaign.brief_version}`}
          </span>
        </div>
      </div>

      <aside className="space-y-4 xl:sticky xl:top-4 xl:self-start">
        <Card title={blockers.length ? "Before drafting can start" : "Ready to draft"}>
          {blockers.length === 0 ? (
            <p className="text-sm text-success">The brief has everything the writer needs.</p>
          ) : (
            <ul className="space-y-2 text-sm">
              {blockers.map((r) => <li key={r.field} className="flex gap-2"><span className="text-danger">●</span>{r.message}</li>)}
            </ul>
          )}
          {recommended.length ? (
            <>
              <h3 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Recommended</h3>
              <ul className="space-y-2 text-sm text-muted">
                {recommended.map((r) => <li key={r.field} className="flex gap-2"><span className="text-warning">●</span>{r.message}</li>)}
              </ul>
            </>
          ) : null}
          <p className="mt-4 text-xs text-muted">Checklist reflects the last saved version.</p>
        </Card>
      </aside>
    </div>
  );
}
