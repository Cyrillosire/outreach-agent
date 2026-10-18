"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Modal } from "@/components/modal";
import { Badge, buttonClass, Card, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import {
  DRAFT_STATUS_LABELS, FIT_LABELS, formatDate, formatScore, SCORE_LABELS, SECTION_LABELS, SECTION_ORDER,
} from "@/lib/labels";
import type { DraftDetail } from "@/lib/types";

const RATIONALE_LABELS: Record<string, string> = {
  why_organization: "Why this organization",
  why_person: "Why this person",
  why_offering: "Why this offering",
  why_now: "Why now",
  next_step: "Proposed next step",
};
const POLL_MS = 3000;
const POLL_LIMIT = 40;

export function DraftReview({ draft }: { draft: DraftDetail }) {
  const router = useRouter();
  const [subject, setSubject] = useState(draft.subject);
  const [sections, setSections] = useState<Record<string, string>>(draft.sections);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "warning" | "danger" | "info"; text: string } | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [rewrite, setRewrite] = useState<{ sections: string[]; instruction: string } | null>(null);
  const [polling, setPolling] = useState(false);

  const editable = draft.is_current && draft.status !== "rejected";
  const changed = subject !== draft.subject || SECTION_ORDER.some((s) => (sections[s] ?? "") !== (draft.sections[s] ?? ""));
  const words = SECTION_ORDER.reduce((n, s) => n + (sections[s] ?? "").split(/\s+/).filter(Boolean).length, 0);
  const errors = draft.validation_issues.filter((i) => i.severity === "error");
  const warnings = draft.validation_issues.filter((i) => i.severity === "warning");
  const q = draft.quality_scores;

  // After a regeneration is queued, wait for the new version and open it.
  useEffect(() => {
    if (!polling) return;
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      try {
        const latest = await apiFetch<DraftDetail>(`/drafts/${draft.id}`);
        const current = latest.versions.find((v) => v.is_current);
        if (current && current.id !== draft.id) {
          clearInterval(timer);
          router.push(`/drafts/${current.id}`);
          return;
        }
      } catch {
        // keep polling; transient errors are fine here
      }
      if (tries >= POLL_LIMIT) {
        clearInterval(timer);
        setPolling(false);
        setNotice({ tone: "warning", text: "Still writing. Check the campaign's contacts tab for progress or blockers." });
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [polling, draft.id, router]);

  async function call<T>(fn: () => Promise<T>, after: (r: T) => void) {
    setBusy(true);
    setNotice(null);
    try {
      after(await fn());
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "That didn't work.") });
    } finally {
      setBusy(false);
    }
  }

  const saveEdit = () => call(
    () => apiFetch<DraftDetail>(`/drafts/${draft.id}`, {
      method: "PATCH",
      body: JSON.stringify({ base_version: draft.version, subject: subject !== draft.subject ? subject : undefined, sections }),
    }),
    (d) => router.push(`/drafts/${d.id}`),
  );
  const approve = () => call(
    () => apiFetch<DraftDetail>(`/drafts/${draft.id}/approve`, {
      method: "POST", body: JSON.stringify({ content_sha256: draft.content_sha256, acknowledge_issues: acknowledge }),
    }),
    () => { setNotice({ tone: "success", text: "Approved." }); router.refresh(); },
  );
  const reject = () => call(
    () => apiFetch(`/drafts/${draft.id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
    () => { setRejecting(false); router.refresh(); },
  );
  const regenerate = (secs: string[], instruction: string) => call(
    () => apiFetch(`/drafts/${draft.id}/regenerate`, {
      method: "POST", body: JSON.stringify({ sections: secs, instruction: instruction.trim() || null }),
    }),
    () => {
      setRewrite(null);
      setPolling(true);
      setNotice({ tone: "info", text: secs.length ? `Rewriting ${secs.map((s) => SECTION_LABELS[s]).join(", ")}…`
        : "Writing a new version…" });
    },
  );

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
      <div className="space-y-4">
        {notice ? <Notice tone={notice.tone} title={notice.text} /> : null}
        {draft.status === "rejected" && draft.rejected_reason ? (
          <Notice tone="danger" title="Rejected">{draft.rejected_reason}</Notice>
        ) : null}

        <Card>
          <Field label="Subject" htmlFor="d-subject">
            <input id="d-subject" className={inputClass} value={subject} disabled={!editable}
                   onChange={(e) => setSubject(e.target.value)} />
          </Field>
          <div className="mt-4 space-y-4">
            {SECTION_ORDER.map((s) => (
              <div key={s}>
                <div className="mb-1 flex items-center justify-between">
                  <label htmlFor={`d-${s}`} className="text-sm font-medium">{SECTION_LABELS[s]}</label>
                  {editable ? (
                    <button type="button" className="text-xs text-muted hover:text-foreground hover:underline"
                            disabled={busy || polling} onClick={() => setRewrite({ sections: [s], instruction: "" })}>
                      Rewrite this part
                    </button>
                  ) : null}
                </div>
                <textarea id={`d-${s}`} rows={s === "value" ? 3 : 2} className={inputClass} disabled={!editable}
                          value={sections[s] ?? ""} onChange={(e) => setSections({ ...sections, [s]: e.target.value })} />
              </div>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
            <span className={words > draft.max_words ? "text-danger" : "text-muted"}>
              {words} / {draft.max_words} words
            </span>
            {editable ? (
              <>
                <button type="button" className={buttonClass("secondary")} disabled={!changed || busy} onClick={saveEdit}>
                  Save as new version
                </button>
                <button type="button" className={buttonClass("ghost")} disabled={!changed}
                        onClick={() => { setSubject(draft.subject); setSections(draft.sections); }}>
                  Discard changes
                </button>
                <button type="button" className={buttonClass("ghost")} disabled={busy || polling}
                        onClick={() => setRewrite({ sections: [], instruction: "" })}>
                  Regenerate whole email
                </button>
              </>
            ) : null}
          </div>
          {changed ? <p className="mt-2 text-xs text-muted">Saving re-checks the draft and removes any approval.</p> : null}
        </Card>

        <Card title="Exactly what will be sent">
          <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed">{draft.body_text}</pre>
          <p className="mt-3 text-xs text-muted">
            Greeting, signature, and compliance footer come from the campaign brief. The approval is tied to this exact text.
          </p>
        </Card>

        {editable && draft.status === "pending_review" ? (
          <Card title="Decision">
            {errors.length ? (
              <label className="mb-3 flex items-start gap-2 text-sm">
                <input type="checkbox" className="mt-0.5" checked={acknowledge} onChange={(e) => setAcknowledge(e.target.checked)} />
                I&apos;ve checked the {errors.length} blocking issue(s) listed and confirm the email is accurate.
              </label>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <button type="button" className={buttonClass("primary")}
                      disabled={busy || polling || changed || (errors.length > 0 && !acknowledge)} onClick={approve}
                      title={changed ? "Save or discard your edits first" : undefined}>
                Approve this version
              </button>
              <button type="button" className={buttonClass("danger")} disabled={busy} onClick={() => setRejecting(true)}>Reject</button>
            </div>
          </Card>
        ) : null}
        {draft.status === "approved" ? (
          <Notice tone="success" title={`Approved ${formatDate(draft.approved_at)}`}>
            Sending is configured in the next milestone (mailbox connection). Editing creates a new version that needs approval again.
          </Notice>
        ) : null}
      </div>

      <aside className="space-y-4">
        <Card title="Why this email">
          <div className="mb-3 flex flex-wrap gap-2">
            {draft.plan.fit ? <Badge tone={FIT_LABELS[draft.plan.fit].tone}>{FIT_LABELS[draft.plan.fit].label}</Badge> : null}
            {draft.plan.persona ? <Badge tone="info">{draft.plan.persona}</Badge> : null}
          </div>
          {draft.plan.fit_reason ? <p className="mb-3 text-sm">{draft.plan.fit_reason}</p> : null}
          {draft.plan.disqualifier_hits?.length ? (
            <div className="mb-3"><Notice tone="danger" title="Possible disqualifiers">{draft.plan.disqualifier_hits.join("; ")}</Notice></div>
          ) : null}
          <dl className="space-y-2 text-sm">
            {draft.plan.opportunity ? (
              <div><dt className="text-xs font-medium uppercase tracking-wide text-muted">Opportunity</dt><dd>{draft.plan.opportunity}</dd></div>
            ) : null}
            {Object.entries(draft.plan.rationale ?? {}).filter(([, v]) => v).map(([k, v]) => (
              <div key={k}><dt className="text-xs font-medium uppercase tracking-wide text-muted">{RATIONALE_LABELS[k] ?? k}</dt><dd>{v}</dd></div>
            ))}
          </dl>
        </Card>

        <Card title={`Quality ${formatScore(q.overall)} / 100`}>
          <div className="mb-3">
            {q.passed ? <Badge tone="success">Passed the quality gate</Badge> : <Badge tone="warning">Below the quality gate</Badge>}
          </div>
          {q.review_summary ? <p className="mb-3 text-sm text-muted">Reviewer: {q.review_summary}</p> : null}
          <ul className="space-y-1.5">
            {Object.entries(q.components ?? {}).map(([k, v]) => (
              <li key={k} className="text-xs">
                <div className="flex justify-between"><span>{SCORE_LABELS[k] ?? k}</span><span className="tabular-nums">{formatScore(v)}</span></div>
                <div className="mt-0.5 h-1.5 rounded bg-surface-muted">
                  <div className={`h-1.5 rounded ${v >= 0.7 ? "bg-success" : v >= 0.5 ? "bg-warning" : "bg-danger"}`}
                       style={{ width: `${Math.round(v * 100)}%` }} />
                </div>
              </li>
            ))}
          </ul>
        </Card>

        {draft.validation_issues.length ? (
          <Card title="Issues">
            <ul className="space-y-2 text-sm">
              {[...errors, ...warnings].map((i, n) => (
                <li key={n} className="flex gap-2">
                  <span className={i.severity === "error" ? "text-danger" : "text-warning"}>●</span>
                  <span>{i.message}</span>
                </li>
              ))}
            </ul>
          </Card>
        ) : null}

        <Card title="Evidence">
          {draft.evidence.length === 0 ? <p className="text-sm text-muted">No research facts were used.</p> : (
            <ul className="space-y-3">
              {draft.evidence.map((e) => (
                <li key={e.fact_id} className="text-sm">
                  <div className="flex items-start gap-2">
                    {e.ref ? <span className="rounded bg-surface-muted px-1.5 text-xs font-mono">{e.ref}</span> : null}
                    <span className={e.cited ? "" : "text-muted"}>{e.claim}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1 pl-1">
                    {e.cited ? <Badge tone="success">Cited</Badge> : <Badge>Available, not used</Badge>}
                    <Badge>{e.subject}</Badge>
                    <Badge>{e.verification_status.replaceAll("_", " ")}</Badge>
                  </div>
                  {e.sources.map((s, i) => (
                    <blockquote key={i} className="mt-1 border-l-2 border-border pl-2 text-xs text-muted">
                      “{s.excerpt}” <a href={s.url} target="_blank" rel="noopener noreferrer" className="underline">
                        {s.publisher ?? s.title ?? "source"}
                      </a>{s.published_at ? `, ${new Date(s.published_at).toLocaleDateString()}` : ""}
                    </blockquote>
                  ))}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Versions">
          <ul className="space-y-1 text-sm">
            {draft.versions.map((v) => (
              <li key={v.id} className="flex items-center justify-between gap-2">
                {v.id === draft.id ? <span className="font-medium">v{v.version} (viewing)</span>
                  : <Link href={`/drafts/${v.id}`} className="hover:underline">v{v.version}</Link>}
                <span className="text-xs text-muted">{v.generated_by === "user" ? "edited" : "AI"} · {formatScore(v.overall_score)}</span>
                <Badge tone={DRAFT_STATUS_LABELS[v.status].tone}>{DRAFT_STATUS_LABELS[v.status].label}</Badge>
              </li>
            ))}
          </ul>
          {draft.generation_metadata.model ? (
            <p className="mt-3 text-xs text-muted">
              Written by {draft.generation_metadata.model} in {draft.generation_metadata.attempts ?? 1} attempt(s) from brief
              v{draft.generation_metadata.brief_version}.
            </p>
          ) : null}
        </Card>
      </aside>

      <Modal open={rejecting} title="Reject this draft?" onClose={() => setRejecting(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setRejecting(false)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy} onClick={reject}>Reject</button>
             </>}>
        <Field label="Reason (optional)" htmlFor="d-reason" hint="Shown on the campaign's contact list. Regenerate later if you change your mind.">
          <textarea id="d-reason" rows={3} className={inputClass} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
      </Modal>

      <Modal open={rewrite !== null} onClose={() => setRewrite(null)}
             title={rewrite?.sections.length ? `Rewrite: ${SECTION_LABELS[rewrite.sections[0]]}` : "Regenerate the whole email"}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setRewrite(null)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy}
                       onClick={() => rewrite && regenerate(rewrite.sections, rewrite.instruction)}>Rewrite</button>
             </>}>
        <Field label="Instruction (optional)" htmlFor="d-instr" hint="e.g. “softer ask”, “mention the rollout timeline”, “shorter”.">
          <input id="d-instr" className={inputClass} value={rewrite?.instruction ?? ""}
                 onChange={(e) => rewrite && setRewrite({ ...rewrite, instruction: e.target.value })} />
        </Field>
        <p className="mt-3 text-xs text-muted">The rewrite uses the same evidence and rules, and creates a new version for review.</p>
      </Modal>
    </div>
  );
}
