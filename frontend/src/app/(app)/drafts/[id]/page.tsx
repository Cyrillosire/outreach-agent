import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { Badge, Notice, PageHeader } from "@/components/ui";
import { serverFetchOrNull } from "@/lib/api-server";
import { DRAFT_STATUS_LABELS, stepLabel } from "@/lib/labels";
import type { DraftDetail } from "@/lib/types";

import { DraftReview } from "./draft-review";

export const metadata: Metadata = { title: "Draft" };

export default async function DraftPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const draft = await serverFetchOrNull<DraftDetail>(`/drafts/${id}`);
  if (!draft) notFound();
  const status = DRAFT_STATUS_LABELS[draft.status];
  const current = draft.versions.find((v) => v.is_current);

  return (
    <>
      <PageHeader
        title={draft.prospect_name ?? "Draft"}
        description={[draft.prospect_title, draft.company_name].filter(Boolean).join(" · ")}
        actions={<div className="flex items-center gap-2 text-sm text-muted">
          <Link href={`/campaigns/${draft.campaign_id}?tab=contacts`} className="hover:underline">{draft.campaign_name}</Link>
          <span>· {stepLabel(draft.step_order)} of {draft.total_steps} · v{draft.version}</span>
          <Badge tone={status.tone}>{status.label}</Badge>
        </div>}
      />
      {!draft.is_current ? (
        <div className="mb-4">
          <Notice tone="warning" title="This is an older version.">
            {current ? <Link href={`/drafts/${current.id}`} className="underline">Open the current version (v{current.version})</Link>
              : "There is no current version."}
          </Notice>
        </div>
      ) : null}
      <DraftReview key={draft.id} draft={draft} />
    </>
  );
}
