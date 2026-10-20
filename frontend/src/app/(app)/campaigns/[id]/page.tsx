import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { Badge, PageHeader, StatTile } from "@/components/ui";
import { serverFetch, serverFetchOrNull, serverFetchPage } from "@/lib/api-server";
import { CAMPAIGN_STATUS_LABELS, MOTION_LABELS } from "@/lib/labels";
import { firstParam, type SearchParams } from "@/lib/query";
import type { Campaign, Enrollment, Mailbox, Offering, PageMeta, Playbook } from "@/lib/types";

import { BriefEditor } from "./brief-editor";
import { CampaignControls } from "./campaign-controls";
import { EnrollmentTable } from "./enrollment-table";

export const metadata: Metadata = { title: "Campaign" };

export default async function CampaignPage({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<SearchParams>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const campaign = await serverFetchOrNull<Campaign>(`/campaigns/${id}`);
  if (!campaign) notFound();
  const [offerings, playbooks, enrollments, mailboxes] = await Promise.all([
    serverFetch<Offering[]>("/offerings"),
    serverFetch<Playbook[]>("/campaigns/playbooks"),
    serverFetchPage<Enrollment[], PageMeta>(`/campaigns/${id}/prospects?page_size=200`),
    serverFetch<Mailbox[]>("/mailboxes"),
  ]);
  const total = enrollments.meta.total;
  const tab = firstParam(query.tab) ?? (campaign.ready && total > 0 ? "contacts" : "brief");
  const status = CAMPAIGN_STATUS_LABELS[campaign.status];
  const counts = campaign.enrollment_counts;
  const tabClass = (t: string) => `border-b-2 px-3 py-2 text-sm ${tab === t
    ? "border-accent font-medium text-foreground" : "border-transparent text-muted hover:text-foreground"}`;

  return (
    <>
      <PageHeader
        title={campaign.name}
        description={`${MOTION_LABELS[campaign.motion]}${campaign.offering_name ? ` · ${campaign.offering_name}` : ""}`}
        actions={<div className="flex items-center gap-2">
          <Badge tone={status.tone}>{status.label}</Badge>
          <CampaignControls campaign={campaign} />
        </div>}
      />

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <StatTile label="Contacts" value={total} />
        <StatTile label="Waiting" value={counts.enrolled ?? 0} hint="research or a blocker" />
        <StatTile label="Drafting" value={counts.awaiting_draft ?? 0} />
        <StatTile label="Needs review" value={counts.awaiting_approval ?? 0} tone={counts.awaiting_approval ? "danger" : "neutral"} />
        <StatTile label="Approved drafts" value={campaign.draft_counts.approved ?? 0} />
      </div>

      <nav className="mb-4 flex gap-1 border-b border-border" aria-label="Campaign sections">
        <Link href="?tab=brief" className={tabClass("brief")} aria-current={tab === "brief" ? "page" : undefined}>
          Brief {campaign.ready ? null : <span className="ml-1 text-warning">•</span>}
        </Link>
        <Link href="?tab=contacts" className={tabClass("contacts")} aria-current={tab === "contacts" ? "page" : undefined}>
          Contacts ({total})
        </Link>
        <Link href={`/drafts?campaign_id=${campaign.id}`} className={tabClass("drafts")}>Drafts</Link>
      </nav>

      {tab === "contacts"
        ? <EnrollmentTable campaign={campaign} rows={enrollments.data} />
        : <BriefEditor campaign={campaign} offerings={offerings} playbooks={playbooks} mailboxes={mailboxes} />}
    </>
  );
}
