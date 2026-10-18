"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Modal } from "@/components/modal";
import { buttonClass, Field, inputClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";

interface CompanyFields {
  name: string;
  normalized_domain: string;
  website: string;
  industry: string;
  location: string;
}

export function CompanyActions({ companyId, initial, contacts }: {
  companyId: string;
  initial: CompanyFields;
  contacts: number;
}) {
  const router = useRouter();
  const [dialog, setDialog] = useState<null | "edit" | "delete">(null);
  const [values, setValues] = useState(initial);
  const [withContacts, setWithContacts] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    const changes: Record<string, string | null> = {};
    (Object.keys(values) as (keyof CompanyFields)[]).forEach((k) => {
      if (values[k].trim() !== initial[k].trim()) changes[k] = values[k].trim() || null;
    });
    if (Object.keys(changes).length === 0) return setDialog(null);
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/companies/${companyId}`, { method: "PATCH", body: JSON.stringify(changes) });
      setDialog(null);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/companies/${companyId}?delete_contacts=${withContacts}`, { method: "DELETE" });
      router.replace("/companies");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed.");
      setBusy(false);
    }
  }

  const fields: { key: keyof CompanyFields; label: string; hint?: string }[] = [
    { key: "name", label: "Name", hint: "Must be unique; merge exact-name duplicates from the Duplicates page." },
    { key: "normalized_domain", label: "Domain", hint: "e.g. example.com" },
    { key: "website", label: "Website" },
    { key: "industry", label: "Industry" },
    { key: "location", label: "Location" },
  ];

  return (
    <div className="flex gap-2">
      <button type="button" className={buttonClass()} onClick={() => setDialog("edit")}>Edit</button>
      <button type="button" className={buttonClass("danger")} onClick={() => setDialog("delete")}>Delete</button>

      <Modal open={dialog === "edit"} title="Edit company" onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("primary")} disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
             </>}>
        <div className="space-y-3">
          {fields.map((f) => (
            <Field key={f.key} label={f.label} htmlFor={`co-${f.key}`} hint={f.hint}>
              <input id={`co-${f.key}`} className={inputClass} value={values[f.key]}
                     onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
            </Field>
          ))}
          {error ? <Notice tone="danger" title={error} /> : null}
        </div>
      </Modal>

      <Modal open={dialog === "delete"} title="Delete this company?" onClose={() => setDialog(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setDialog(null)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy} onClick={remove}>
                 {busy ? "Deleting…" : "Delete permanently"}
               </button>
             </>}>
        <p className="text-sm">The company record and its research are removed permanently.</p>
        <label className="mt-3 flex items-start gap-2 text-sm">
          <input type="checkbox" checked={withContacts} onChange={(e) => setWithContacts(e.target.checked)} />
          <span>Also delete its {contacts} contact(s). Otherwise they&apos;re kept without a company.</span>
        </label>
        {error ? <div className="mt-3"><Notice tone="danger" title={error} /></div> : null}
      </Modal>
    </div>
  );
}
