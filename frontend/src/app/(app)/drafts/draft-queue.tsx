"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { BulkBar, useSelectionHelpers } from "@/components/bulk-bar";
import { Modal } from "@/components/modal";
import { Badge, buttonClass, Cell, DataTable, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { DRAFT_STATUS_LABELS, FIT_LABELS, formatScore, stepLabel } from "@/lib/labels";
import type { DraftListItem } from "@/lib/types";

export function DraftQueue({ rows, empty }: { rows: DraftListItem[]; empty: React.ReactNode }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { allOnPage, toggle, toggleAll } = useSelectionHelpers(rows, selected, setSelected);
  const [confirm, setConfirm] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "success" | "warning" | "danger"; text: string } | null>(null);
  const selectable = rows.filter((r) => r.status === "pending_review");
  const flagged = rows.filter((r) => selected.has(r.id) && r.has_errors).length;

  async function bulkApprove() {
    setBusy(true);
    setNotice(null);
    try {
      const r = await apiFetch<{ approved: string[]; skipped: { id: string; reason: string }[] }>("/drafts/bulk-approve", {
        method: "POST", body: JSON.stringify({ draft_ids: [...selected], confirm_reviewed: reviewed }),
      });
      setNotice(r.skipped.length
        ? { tone: "warning", text: `Approved ${r.approved.length}. Skipped ${r.skipped.length}: ${r.skipped[0].reason}` }
        : { tone: "success", text: `Approved ${r.approved.length} draft(s).` });
      setSelected(new Set());
      setConfirm(false);
      setReviewed(false);
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Bulk approval failed.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {notice ? <div className="mb-3"><Notice tone={notice.tone} title={notice.text} /></div> : null}
      <BulkBar count={selected.size} onClear={() => setSelected(new Set())}>
        <button type="button" className={buttonClass("primary")} onClick={() => setConfirm(true)}>Approve selected</button>
      </BulkBar>
      <DataTable
        head={[
          <input key="all" type="checkbox" aria-label="Select all on this page" checked={allOnPage}
                 disabled={selectable.length === 0} onChange={toggleAll} />,
          "Contact", "Subject", "Campaign", "Email", "Score", "Fit", "Status",
        ]}
        empty={rows.length === 0 ? empty : null}
      >
        {rows.map((d) => (
          <tr key={d.id} className={selected.has(d.id) ? "bg-info-bg/40" : "hover:bg-surface-muted/60"}>
            <Cell>
              <input type="checkbox" aria-label={`Select draft for ${d.prospect_name ?? d.prospect_email ?? "contact"}`}
                     disabled={d.status !== "pending_review"} checked={selected.has(d.id)} onChange={() => toggle(d.id)} />
            </Cell>
            <Cell>
              <div className="font-medium">{d.prospect_name ?? d.prospect_email ?? "Unnamed"}</div>
              <div className="text-xs text-muted">{d.company_name ?? d.prospect_email}</div>
            </Cell>
            <Cell>
              <Link href={`/drafts/${d.id}`} className="font-medium hover:underline">{d.subject}</Link>
              {d.has_errors ? <div className="text-xs text-danger">Has blocking issues</div>
                : d.issue_count ? <div className="text-xs text-warning">{d.issue_count} warning(s)</div> : null}
            </Cell>
            <Cell className="text-muted">{d.campaign_name}</Cell>
            <Cell className="whitespace-nowrap text-muted">{stepLabel(d.step_order)} · v{d.version}</Cell>
            <Cell className="tabular-nums">{formatScore(d.overall_score)}</Cell>
            <Cell>{d.fit ? <Badge tone={FIT_LABELS[d.fit].tone}>{FIT_LABELS[d.fit].label}</Badge> : "—"}</Cell>
            <Cell><Badge tone={DRAFT_STATUS_LABELS[d.status].tone}>{DRAFT_STATUS_LABELS[d.status].label}</Badge></Cell>
          </tr>
        ))}
      </DataTable>

      <Modal open={confirm} title={`Approve ${selected.size} draft(s)?`} onClose={() => setConfirm(false)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirm(false)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy || !reviewed} onClick={bulkApprove}>
                 {busy ? "Approving…" : "Approve"}
               </button>
             </>}>
        {flagged ? (
          <div className="mb-3"><Notice tone="warning" title={`${flagged} selected draft(s) have blocking issues and will be skipped.`}>
            Open them individually to fix or acknowledge the issues.
          </Notice></div>
        ) : null}
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
          I&apos;ve read each selected draft and checked its claims against the evidence.
        </label>
      </Modal>
    </>
  );
}
