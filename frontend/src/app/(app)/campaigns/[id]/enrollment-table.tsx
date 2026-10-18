"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { BulkBar, useSelectionHelpers } from "@/components/bulk-bar";
import { Modal } from "@/components/modal";
import { Badge, buttonClass, Cell, DataTable, EmptyState, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { DRAFT_STATUS_LABELS, ENROLLMENT_LABELS, FIT_LABELS, formatScore, RESEARCH_LABELS } from "@/lib/labels";
import type { Campaign, Enrollment } from "@/lib/types";

export function EnrollmentTable({ campaign, rows }: { campaign: Campaign; rows: Enrollment[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { allOnPage, toggle, toggleAll } = useSelectionHelpers(rows.map((r) => ({ ...r, id: r.prospect_id })),
                                                               selected, setSelected);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const ids = [...selected];

  async function post(path: string, body: unknown, done: (data: unknown) => string) {
    setBusy(true);
    setNotice(null);
    try {
      const data = await apiFetch<unknown>(path, { method: "POST", body: JSON.stringify(body) });
      setNotice({ tone: "success", text: done(data) });
      setSelected(new Set());
      setConfirmRemove(false);
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "That didn't work.") });
    } finally {
      setBusy(false);
    }
  }

  const draftSelected = (force: boolean) => post(`/campaigns/${campaign.id}/generate`,
    { prospect_ids: ids, force },
    (d) => Object.entries(d as Record<string, number>).map(([k, n]) => `${n} ${k.replaceAll("_", " ")}`).join(" · ")
      || "Nothing to draft for the selected contacts.");

  return (
    <>
      {notice ? <div className="mb-3"><Notice tone={notice.tone} title={notice.text} /></div> : null}
      <BulkBar count={selected.size} onClear={() => setSelected(new Set())}>
        <button type="button" className={buttonClass()} disabled={busy || !campaign.ready} onClick={() => draftSelected(false)}>
          Draft selected
        </button>
        <button type="button" className={buttonClass()} disabled={busy || !campaign.ready} onClick={() => draftSelected(true)}
                title="Draft even if research isn't complete or the fit looks weak">
          Draft anyway
        </button>
        <button type="button" className={buttonClass("danger")} onClick={() => setConfirmRemove(true)}>Remove from campaign</button>
      </BulkBar>

      <DataTable
        head={[
          <input key="all" type="checkbox" aria-label="Select all" checked={allOnPage} onChange={toggleAll} />,
          "Contact", "Research", "Status", "Fit", "First email", "What's next",
        ]}
        empty={rows.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No contacts in this campaign">
              Select contacts on the <Link href="/prospects" className="underline">Prospects</Link> page and choose
              “Add to campaign”.
            </EmptyState>
          </div>
        ) : null}
      >
        {rows.map((r) => (
          <tr key={r.id} className={selected.has(r.prospect_id) ? "bg-info-bg/40" : "hover:bg-surface-muted/60"}>
            <Cell>
              <input type="checkbox" aria-label={`Select ${r.full_name ?? r.email ?? "contact"}`}
                     checked={selected.has(r.prospect_id)} onChange={() => toggle(r.prospect_id)} />
            </Cell>
            <Cell>
              <Link href={`/prospects/${r.prospect_id}`} className="font-medium hover:underline">
                {r.full_name ?? r.email ?? "Unnamed"}
              </Link>
              <div className="text-xs text-muted">{[r.title, r.company_name].filter(Boolean).join(" · ") || r.email}</div>
            </Cell>
            <Cell><Badge tone={RESEARCH_LABELS[r.research_status].tone}>{RESEARCH_LABELS[r.research_status].label}</Badge></Cell>
            <Cell><Badge tone={ENROLLMENT_LABELS[r.state].tone}>{ENROLLMENT_LABELS[r.state].label}</Badge></Cell>
            <Cell>
              {r.fit ? <span title={r.fit_assessment?.reason}><Badge tone={FIT_LABELS[r.fit].tone}>{FIT_LABELS[r.fit].label}</Badge></span>
                : <span className="text-muted">—</span>}
            </Cell>
            <Cell>
              {r.draft_id && r.draft_status ? (
                <Link href={`/drafts/${r.draft_id}`} className="hover:underline">
                  <Badge tone={DRAFT_STATUS_LABELS[r.draft_status].tone}>{DRAFT_STATUS_LABELS[r.draft_status].label}</Badge>
                  <span className="ml-2 text-xs tabular-nums text-muted">score {formatScore(r.draft_score)}</span>
                  {r.draft_issues ? <span className="ml-1 text-xs text-warning">· {r.draft_issues} issue(s)</span> : null}
                </Link>
              ) : <span className="text-muted">—</span>}
            </Cell>
            <Cell className="max-w-sm text-xs text-muted">{r.blocker ?? (r.state === "awaiting_draft" ? "Writing the draft…" : "")}</Cell>
          </tr>
        ))}
      </DataTable>

      <Modal open={confirmRemove} title={`Remove ${selected.size} contact(s) from this campaign?`}
             onClose={() => setConfirmRemove(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirmRemove(false)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy}
                       onClick={() => post(`/campaigns/${campaign.id}/remove`, { prospect_ids: ids },
                                           (d) => `Removed ${(d as { removed: number }).removed} contact(s).`)}>
                 Remove
               </button>
             </>}>
        <p className="text-sm">Their unsent drafts for this campaign are discarded. The contacts themselves are kept.</p>
      </Modal>
    </>
  );
}
