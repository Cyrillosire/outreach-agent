import type { Metadata } from "next";
import Link from "next/link";

import { Badge, buttonClass, Cell, DataTable, EmptyState, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { OFFERING_TYPE_LABELS } from "@/lib/labels";
import type { Offering } from "@/lib/types";

export const metadata: Metadata = { title: "Offerings" };

export default async function OfferingsPage() {
  const offerings = await serverFetch<Offering[]>("/offerings?include_archived=true");
  return (
    <>
      <PageHeader
        title="Offerings"
        description="What you sell or propose. Each campaign promotes one offering, and drafts may only describe it using the capabilities and proof points defined here."
        actions={<Link href="/offerings/new" className={buttonClass("primary")}>New offering</Link>}
      />
      <DataTable
        head={["Name", "Type", "Category", "Capabilities", "Proof points", "Updated"]}
        empty={offerings.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No offerings yet">
              Define what you&apos;re selling (for example clinical documentation, or a partnership program) before
              creating a campaign.
            </EmptyState>
          </div>
        ) : null}
      >
        {offerings.map((o) => (
          <tr key={o.id} className="hover:bg-surface-muted/60">
            <Cell>
              <Link href={`/offerings/${o.id}`} className="font-medium hover:underline">{o.name}</Link>
              {o.archived ? <span className="ml-2"><Badge>Archived</Badge></span> : null}
              <div className="max-w-md truncate text-xs text-muted">{o.summary}</div>
            </Cell>
            <Cell>{OFFERING_TYPE_LABELS[o.offering_type]}</Cell>
            <Cell className="text-muted">{o.category ?? "—"}</Cell>
            <Cell className="tabular-nums">{o.details.capabilities.length}</Cell>
            <Cell className="tabular-nums">{o.details.proof_points.length}</Cell>
            <Cell className="whitespace-nowrap text-muted">{new Date(o.updated_at).toLocaleDateString()}</Cell>
          </tr>
        ))}
      </DataTable>
    </>
  );
}
