"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { BulkBar, useSelectionHelpers } from "@/components/bulk-bar";
import { Modal } from "@/components/modal";
import { Badge, buttonClass, Cell, DataTable, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import { formatDate, OUTREACH_LABELS, RESEARCH_LABELS } from "@/lib/labels";
import type { ProspectListItem } from "@/lib/types";

const EDITABLE = [
  { key: "title", label: "Job title" },
  { key: "industry", label: "Industry" },
  { key: "country", label: "Country / region" },
  { key: "location", label: "Location" },
  { key: "company_name", label: "Company name (moves them to that company)" },
] as const;

interface BulkResult { affected: number; failed: { id: string; error: string }[] }

export function ProspectTable({ rows, empty }: { rows: ProspectListItem[]; empty: React.ReactNode }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { allOnPage, toggle, toggleAll } = useSelectionHelpers(rows, selected, setSelected);
  const [dialog, setDialog] = useState<null | "edit" | "delete">(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ tone: "success" | "danger" | "warning"; text: string } | null>(null);
  const [fields, setFields] = useState<Record<string, { on: boolean; value: string }>>({});
  const [attr, setAttr] = useState({ key: "", value: "" });

  const ids = [...selected];

  async function run(label: string, fn: () => Promise<BulkResult>) {
    setBusy(true);
    setResult(null);
    try {
      const r = await fn();
      setResult(r.failed?.length
        ? { tone: "warning", text: `${label}: ${r.affected} done, ${r.failed.length} skipped (${r.failed[0].error})` }
        : { tone: "success", text: `${label}: ${r.affected} record(s).` });
      setSelected(new Set());
      setDialog(null);
      router.refresh();
    } catch (err) {
      setResult({ tone: "danger", text: err instanceof ApiError ? err.message : `${label} failed.` });
    } finally {
      setBusy(false);
    }
  }

  const bulkEdit = () => {
    const changes: Record<string, unknown> = {};
    for (const f of EDITABLE) {
      const v = fields[f.key];
      if (v?.on) changes[f.key] = v.value.trim() || null;
    }
    if (attr.key.trim()) changes.attributes_set = { [attr.key.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_")]: attr.value };
    return run("Updated", () => apiFetch<BulkResult>("/prospects/bulk-update", {
      method: "POST", body: JSON.stringify({ ids, changes }),
    }));
  };

  return (
    <>
      {result ? <div className="mb-3"><Notice tone={result.tone} title={result.text} /></div> : null}
      <BulkBar count={selected.size} onClear={() => setSelected(new Set())}>
        <button type="button" className={buttonClass()} onClick={() => setDialog("edit")}>Edit fields</button>
        <button type="button" className={buttonClass()} disabled={busy}
                onClick={() => run("Research queued", () => apiFetch<BulkResult>("/prospects/bulk-research", {
                  method: "POST", body: JSON.stringify({ ids }),
                }))}>Start research</button>
        <button type="button" className={buttonClass("danger")} onClick={() => setDialog("delete")}>Delete</button>
      </BulkBar>

      <DataTable
        head={[
          <input key="all" type="checkbox" aria-label="Select all on this page" checked={allOnPage} onChange={toggleAll} />,
          "Name", "Title", "Company", "Research", "Outreach", "Next action", "Added",
        ]}
        empty={rows.length === 0 ? empty : null}
      >
        {rows.map((p) => (
          <tr key={p.id} className={selected.has(p.id) ? "bg-info-bg/40" : "hover:bg-surface-muted/60"}>
            <Cell>
              <input type="checkbox" aria-label={`Select ${p.full_name ?? p.email ?? "prospect"}`}
                     checked={selected.has(p.id)} onChange={() => toggle(p.id)} />
            </Cell>
            <Cell>
              <Link href={`/prospects/${p.id}`} className="font-medium text-foreground hover:underline">
                {p.full_name ?? p.email ?? "Unnamed prospect"}
              </Link>
              <div className="text-xs text-muted">{p.email ?? <Badge tone="warning">No email</Badge>}</div>
            </Cell>
            <Cell className="text-muted">{p.title ?? "—"}</Cell>
            <Cell>
              {p.company_id ? <Link href={`/companies/${p.company_id}`} className="hover:underline">{p.company_name}</Link>
                : <span className="text-muted">—</span>}
              {p.company_domain ? <div className="text-xs text-muted">{p.company_domain}</div> : null}
            </Cell>
            <Cell><Badge tone={RESEARCH_LABELS[p.research_status].tone}>{RESEARCH_LABELS[p.research_status].label}</Badge></Cell>
            <Cell><Badge tone={OUTREACH_LABELS[p.outreach_status].tone}>{OUTREACH_LABELS[p.outreach_status].label}</Badge></Cell>
            <Cell className="whitespace-nowrap text-muted">{p.next_action_at ? formatDate(p.next_action_at) : "—"}</Cell>
            <Cell className="whitespace-nowrap text-muted">{new Date(p.created_at).toLocaleDateString()}</Cell>
          </tr>
        ))}
      </DataTable>

      <Modal open={dialog === "edit"} title={`Edit ${selected.size} contact(s)`} onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy} onClick={bulkEdit}>
                 {busy ? "Saving…" : "Apply to selected"}
               </button>
             </>}>
        <p className="mb-3 text-sm text-muted">Tick the fields to change. A ticked field left empty is cleared.</p>
        <div className="space-y-3">
          {EDITABLE.map((f) => (
            <div key={f.key} className="flex items-center gap-2">
              <input type="checkbox" id={`chk-${f.key}`} checked={fields[f.key]?.on ?? false}
                     onChange={(e) => setFields({ ...fields, [f.key]: { on: e.target.checked, value: fields[f.key]?.value ?? "" } })} />
              <label htmlFor={`chk-${f.key}`} className="w-56 shrink-0 text-sm">{f.label}</label>
              <input aria-label={f.label} className={inputClass} disabled={!fields[f.key]?.on}
                     value={fields[f.key]?.value ?? ""}
                     onChange={(e) => setFields({ ...fields, [f.key]: { on: true, value: e.target.value } })} />
            </div>
          ))}
          <div className="flex items-center gap-2">
            <span className="w-56 shrink-0 pl-6 text-sm">Custom attribute</span>
            <input aria-label="Attribute name" placeholder="name (e.g. tier)" className={inputClass} value={attr.key}
                   onChange={(e) => setAttr({ ...attr, key: e.target.value })} />
            <input aria-label="Attribute value" placeholder="value" className={inputClass} value={attr.value}
                   onChange={(e) => setAttr({ ...attr, value: e.target.value })} />
          </div>
        </div>
      </Modal>

      <Modal open={dialog === "delete"} title={`Delete ${selected.size} contact(s)?`} onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy}
                       onClick={() => run("Deleted", () => apiFetch<BulkResult>("/prospects/bulk-delete", {
                         method: "POST", body: JSON.stringify({ ids }),
                       }))}>
                 {busy ? "Deleting…" : "Delete permanently"}
               </button>
             </>}>
        <p className="text-sm">This permanently removes their research, drafts, campaign memberships, and message history.</p>
        <p className="mt-2 text-sm text-muted">
          Unsubscribes and other suppressions are kept, so a contact who opted out stays opted out if re-imported.
        </p>
      </Modal>
    </>
  );
}
