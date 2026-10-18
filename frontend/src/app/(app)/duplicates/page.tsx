import type { Metadata } from "next";

import { EmptyState, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import type { DuplicateCompanyGroup, DuplicateContactGroup } from "@/lib/types";

import { DuplicateGroupCard } from "./duplicate-group";

export const metadata: Metadata = { title: "Duplicates" };

export default async function DuplicatesPage() {
  const data = await serverFetch<{ companies: DuplicateCompanyGroup[]; contacts: DuplicateContactGroup[] }>("/duplicates");
  return (
    <>
      <PageHeader
        title="Duplicates"
        description="Companies with exactly the same name, and contacts with exactly the same name at the same company (case and spacing ignored). New imports and entries are already de-duplicated by these rules; this page handles records that existed before."
      />
      <h2 className="mb-2 text-sm font-semibold">Companies ({data.companies.length} group{data.companies.length === 1 ? "" : "s"})</h2>
      {data.companies.length === 0 ? <EmptyState title="No duplicate companies" /> : (
        <div className="space-y-4">
          {data.companies.map((g) => <DuplicateGroupCard key={g.key} kind="company" group={g} />)}
        </div>
      )}
      <h2 className="mb-2 mt-8 text-sm font-semibold">Contacts ({data.contacts.length} group{data.contacts.length === 1 ? "" : "s"})</h2>
      {data.contacts.length === 0 ? <EmptyState title="No duplicate contacts" /> : (
        <div className="space-y-4">
          {data.contacts.map((g) => <DuplicateGroupCard key={g.key} kind="contact" group={g} />)}
        </div>
      )}
    </>
  );
}
