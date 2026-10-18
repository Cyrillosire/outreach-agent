import type { Metadata } from "next";
import Link from "next/link";

import { Badge, buttonClass, Cell, DataTable, EmptyState, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { CAMPAIGN_STATUS_LABELS, MOTION_LABELS } from "@/lib/labels";
import type { Campaign } from "@/lib/types";

export const metadata: Metadata = { title: "Campaigns" };

export default async function CampaignsPage() {
  const campaigns = await serverFetch<Campaign[]>("/campaigns");
  return (
    <>
      <PageHeader
        title="Campaigns"
        description="Each campaign defines what you're offering, to whom, why it matters to them, and what you're asking for. Drafts are written from this brief plus each contact's research."
        actions={<Link href="/campaigns/new" className={buttonClass("primary")}>New campaign</Link>}
      />
      <DataTable
        head={["Campaign", "Approach", "Offering", "Status", "Contacts", "Drafts to review", "Brief"]}
        empty={campaigns.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No campaigns yet">Start from a playbook: product sale, partnership, pilot, and more.</EmptyState>
          </div>
        ) : null}
      >
        {campaigns.map((c) => {
          const contacts = Object.values(c.enrollment_counts).reduce((a, b) => a + b, 0);
          const blockers = c.readiness.filter((r) => r.severity === "blocker").length;
          return (
            <tr key={c.id} className="hover:bg-surface-muted/60">
              <Cell>
                <Link href={`/campaigns/${c.id}`} className="font-medium hover:underline">{c.name}</Link>
                <div className="max-w-sm truncate text-xs text-muted">{c.brief.goal || "No goal set"}</div>
              </Cell>
              <Cell>{MOTION_LABELS[c.motion]}</Cell>
              <Cell className="text-muted">{c.offering_name ?? "—"}</Cell>
              <Cell><Badge tone={CAMPAIGN_STATUS_LABELS[c.status].tone}>{CAMPAIGN_STATUS_LABELS[c.status].label}</Badge></Cell>
              <Cell className="tabular-nums">{contacts}</Cell>
              <Cell className="tabular-nums">{c.draft_counts.pending_review ?? 0}</Cell>
              <Cell>
                {c.ready ? <Badge tone="success">Complete</Badge>
                  : <Badge tone="warning">{blockers} item{blockers === 1 ? "" : "s"} missing</Badge>}
              </Cell>
            </tr>
          );
        })}
      </DataTable>
    </>
  );
}
