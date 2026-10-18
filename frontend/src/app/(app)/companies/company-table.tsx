"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { BulkBar, useSelectionHelpers } from "@/components/bulk-bar";
import { Modal } from "@/components/modal";
import { Badge, buttonClass, Cell, DataTable, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import { RESEARCH_LABELS } from "@/lib/labels";
import type { CompanyListItem } from "@/lib/types";

interface BulkResult { affected: number; failed?: { id: string; error: string }[] }

export function CompanyTable({ rows, empty }: { rows: CompanyListItem[]; empty: React.ReactNode }) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const { allOnPage, toggle, toggleAll } = useSelectionHelpers(rows, selected, setSelected);
  const [dialog, setDialog] = useState<null | "edit" | "delete">(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ tone: "success" | "danger" | "warning"; text: string } | null>(null);
  const [edit, setEdit] = useState({ industryOn: false, industry: "", locationOn: false, location: "" });
  const [withContacts, setWithContacts] = useState(false);

  const ids = [...selected];
  const contactsSelected = rows.filter((r) => selected.has(r.id)).reduce((n, r) => n + r.contacts, 0);

  async function run(label: string, path: string, body: unknown) {
    setBusy(true);
    setResult(null);
    try {
      const r = await apiFetch<BulkResult>(path, { method: "POST", body: JSON.stringify(body) });
      setResult(r.failed?.length
        ? { tone: "warning", text: `${label}: ${r.affected} done, ${r.failed.length} skipped (${r.failed[0].error})` }
        : { tone: "success", text: `${label}: ${r.affected} compan${r.affected === 1 ? "y" : "ies"}.` });
      setSelected(new Set());
      setDialog(null);
      router.refresh();
    } catch (err) {
      setResult({ tone: "danger", text: err instanceof ApiError ? err.message : `${label} failed.` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {result ? <div className="mb-3"><Notice tone={result.tone} title={result.text} /></div> : null}
      <BulkBar count={selected.size} onClear={() => setSelected(new Set())}>
        <button type="button" className={buttonClass()} onClick={() => setDialog("edit")}>Edit fields</button>
        <button type="button" className={buttonClass("danger")} onClick={() => setDialog("delete")}>Delete</button>
      </BulkBar>
      <DataTable
        head={[<input key="all" type="checkbox" aria-label="Select all on this page" checked={allOnPage} onChange={toggleAll} />,
               "Company", "Domain", "Industry", "Contacts", "Research", "Last researched"]}
        empty={rows.length === 0 ? empty : null}
      >
        {rows.map((c) => (
          <tr key={c.id} className={selected.has(c.id) ? "bg-info-bg/40" : "hover:bg-surface-muted/60"}>
            <Cell><input type="checkbox" aria-label={`Select ${c.name}`} checked={selected.has(c.id)} onChange={() => toggle(c.id)} /></Cell>
            <Cell><Link href={`/companies/${c.id}`} className="font-medium hover:underline">{c.name}</Link></Cell>
            <Cell className="text-muted">{c.normalized_domain ?? "—"}</Cell>
            <Cell className="text-muted">{c.industry ?? "—"}</Cell>
            <Cell className="tabular-nums">{c.contacts}</Cell>
            <Cell><Badge tone={RESEARCH_LABELS[c.research_status].tone}>{RESEARCH_LABELS[c.research_status].label}</Badge></Cell>
            <Cell className="whitespace-nowrap text-muted">
              {c.last_researched_at ? new Date(c.last_researched_at).toLocaleDateString() : "—"}
            </Cell>
          </tr>
        ))}
      </DataTable>

      <Modal open={dialog === "edit"} title={`Edit ${selected.size} compan${selected.size === 1 ? "y" : "ies"}`}
             onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy || (!edit.industryOn && !edit.locationOn)}
                       onClick={() => run("Updated", "/companies/bulk-update", { ids, changes: {
                         ...(edit.industryOn ? { industry: edit.industry.trim() || null } : {}),
                         ...(edit.locationOn ? { location: edit.location.trim() || null } : {}),
                       } })}>
                 {busy ? "Saving…" : "Apply to selected"}
               </button>
             </>}>
        <p className="mb-3 text-sm text-muted">Tick the fields to change. A ticked field left empty is cleared.
          Names and domains are edited one company at a time.</p>
        {(["industry", "location"] as const).map((f) => (
          <div key={f} className="mb-3 flex items-center gap-2">
            <input type="checkbox" id={`c-${f}`} checked={edit[`${f}On`]}
                   onChange={(e) => setEdit({ ...edit, [`${f}On`]: e.target.checked })} />
            <label htmlFor={`c-${f}`} className="w-24 text-sm capitalize">{f}</label>
            <input aria-label={f} className={inputClass} disabled={!edit[`${f}On`]} value={edit[f]}
                   onChange={(e) => setEdit({ ...edit, [f]: e.target.value })} />
          </div>
        ))}
      </Modal>

      <Modal open={dialog === "delete"} title={`Delete ${selected.size} compan${selected.size === 1 ? "y" : "ies"}?`}
             onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy}
                       onClick={() => run("Deleted", "/companies/bulk-delete", { ids, delete_contacts: withContacts })}>
                 {busy ? "Deleting…" : "Delete permanently"}
               </button>
             </>}>
        <p className="text-sm">Company research and records are removed permanently.</p>
        <label className="mt-3 flex items-start gap-2 text-sm">
          <input type="checkbox" checked={withContacts} onChange={(e) => setWithContacts(e.target.checked)} />
          <span>Also delete their {contactsSelected} contact(s). Otherwise contacts are kept without a company, and
            any drafts that relied on company research are withdrawn.</span>
        </label>
      </Modal>
    </>
  );
}
