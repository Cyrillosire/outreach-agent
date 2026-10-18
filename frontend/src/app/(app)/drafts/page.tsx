import type { Metadata } from "next";

import { Pagination } from "@/components/pagination";
import { EmptyState, inputClass, PageHeader } from "@/components/ui";
import { serverFetch, serverFetchPage } from "@/lib/api-server";
import { DRAFT_STATUS_LABELS } from "@/lib/labels";
import { buildQuery, firstParam, pageParam, type SearchParams } from "@/lib/query";
import type { Campaign, DraftListItem, DraftStatus, PageMeta } from "@/lib/types";

import { DraftQueue } from "./draft-queue";

export const metadata: Metadata = { title: "Draft review" };

const FILTER_KEYS = ["campaign_id", "status", "flagged", "step"];
const PAGE_SIZE = 50;

export default async function DraftsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const page = pageParam(params.page);
  // Default view: what needs a decision. "all" removes the status filter.
  const status = firstParam(params.status) ?? "pending_review";
  const apiQuery = buildQuery({ ...params, status: status === "all" ? undefined : status },
                              [...FILTER_KEYS, "page", "page_size"],
                              { page: String(page), page_size: String(PAGE_SIZE) });
  const [{ data: drafts, meta }, campaigns] = await Promise.all([
    serverFetchPage<DraftListItem[], PageMeta>(`/drafts${apiQuery}`),
    serverFetch<Campaign[]>("/campaigns"),
  ]);

  return (
    <>
      <PageHeader
        title="Draft review"
        description="Nothing is sent without approval. Open a draft to see the evidence behind each claim, its quality scores, and any issues. Editing a draft creates a new version and removes any earlier approval."
      />
      <form method="get" className="mb-4 grid gap-2 rounded-lg border border-border bg-surface p-3 sm:grid-cols-2 lg:grid-cols-5">
        <select name="campaign_id" defaultValue={firstParam(params.campaign_id) ?? ""} aria-label="Campaign" className={inputClass}>
          <option value="">All campaigns</option>
          {campaigns.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select name="status" defaultValue={status} aria-label="Status" className={inputClass}>
          {(["pending_review", "approved", "rejected"] as DraftStatus[]).map((s) => (
            <option key={s} value={s}>{DRAFT_STATUS_LABELS[s].label}</option>
          ))}
          <option value="all">Any status</option>
        </select>
        <select name="flagged" defaultValue={firstParam(params.flagged) ?? ""} aria-label="Issues" className={inputClass}>
          <option value="">With or without issues</option>
          <option value="true">Has blocking issues</option>
          <option value="false">No blocking issues</option>
        </select>
        <select name="step" defaultValue={firstParam(params.step) ?? ""} aria-label="Step" className={inputClass}>
          <option value="">Any email</option>
          <option value="0">First emails</option>
          {[1, 2, 3, 4].map((s) => <option key={s} value={s}>Follow-up {s}</option>)}
        </select>
        <button type="submit" className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-accent-foreground">Filter</button>
      </form>

      <DraftQueue rows={drafts} empty={<div className="p-4"><EmptyState title="No drafts here">
        Drafts appear once a campaign&apos;s contacts have finished research and the brief is complete.
      </EmptyState></div>} />
      <Pagination page={page} pageSize={PAGE_SIZE} total={meta.total}
                  hrefFor={(p) => `/drafts${buildQuery(params, FILTER_KEYS, { page: String(p) })}`} />
    </>
  );
}
