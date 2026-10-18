"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge, buttonClass, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError } from "@/lib/envelope";
import { RESEARCH_LABELS } from "@/lib/labels";
import type { DuplicateCompanyGroup, DuplicateContactGroup } from "@/lib/types";

type Group = { kind: "company"; group: DuplicateCompanyGroup } | { kind: "contact"; group: DuplicateContactGroup };

export function DuplicateGroupCard(props: Group) {
  const router = useRouter();
  const records = props.group.records;
  const [keep, setKeep] = useState(records[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const others = records.filter((r) => r.id !== keep).map((r) => r.id);
  const noun = props.kind === "company" ? "company" : "contact";

  async function act(action: "merge" | "delete") {
    const verb = action === "merge" ? `Merge ${others.length} ${noun}(s) into the kept record` :
      `Permanently delete ${others.length} ${noun}(s), keeping only the selected record`;
    if (!window.confirm(`${verb}?`)) return;
    setBusy(true);
    setError(null);
    try {
      if (action === "merge") {
        await apiFetch(props.kind === "company" ? "/companies/merge" : "/prospects/merge", {
          method: "POST", body: JSON.stringify({ survivor_id: keep, duplicate_ids: others }),
        });
      } else {
        await apiFetch(props.kind === "company" ? "/companies/bulk-delete" : "/prospects/bulk-delete", {
          method: "POST", body: JSON.stringify({ ids: others, ...(props.kind === "company" ? { delete_contacts: false } : {}) }),
        });
      }
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That didn't work. Try again.");
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      {props.kind === "company" && props.group.domain_conflict ? (
        <div className="mb-3">
          <Notice tone="warning" title="Same name, different websites">
            These may be different organizations that share a name, or regional sites of one organization. Merge only if
            you know they&apos;re the same; their research will be combined.
          </Notice>
        </div>
      ) : null}
      <fieldset>
        <legend className="mb-2 text-sm font-medium">Choose the record to keep</legend>
        <ul className="space-y-2">
          {records.map((r) => (
            <li key={r.id}>
              <label className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm ${keep === r.id ? "border-accent bg-info-bg/30" : "border-border"}`}>
                <input type="radio" name={`keep-${props.group.key}`} checked={keep === r.id} onChange={() => setKeep(r.id)} className="mt-1" />
                {"name" in r ? (
                  <span className="grid flex-1 gap-1 sm:grid-cols-4">
                    <Link href={`/companies/${r.id}`} className="font-medium hover:underline">{r.name}</Link>
                    <span className="text-muted">{r.normalized_domain ?? "no domain"}</span>
                    <span className="text-muted">{r.contacts} contact(s)</span>
                    <span><Badge tone={RESEARCH_LABELS[r.research_status].tone}>{RESEARCH_LABELS[r.research_status].label}</Badge></span>
                  </span>
                ) : (
                  <span className="grid flex-1 gap-1 sm:grid-cols-4">
                    <Link href={`/prospects/${r.id}`} className="font-medium hover:underline">{r.full_name}</Link>
                    <span className="break-all text-muted">{r.email ?? "no email"}</span>
                    <span className="text-muted">{r.title ?? "—"} · {r.company_name}</span>
                    <span className="text-xs text-muted">added {new Date(r.created_at).toLocaleDateString()} via {r.source.replace("_", " ")}</span>
                  </span>
                )}
              </label>
            </li>
          ))}
        </ul>
      </fieldset>
      {error ? <p className="mt-2 text-sm text-danger">{error}</p> : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={() => act("merge")} className={buttonClass("primary")}>
          Merge into kept record
        </button>
        <button type="button" disabled={busy} onClick={() => act("delete")} className={buttonClass("danger")}>
          Delete the others
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        Merging keeps the selected record&apos;s values, fills its blank fields from the others, and moves {props.kind === "company"
          ? "their contacts" : "their campaign memberships and message history"} over.
      </p>
    </section>
  );
}
