import type { Metadata } from "next";
import Link from "next/link";

import { Badge, Card, EmptyState, PageHeader, StatTile, type Tone } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { RESEARCH_LABELS } from "@/lib/labels";
import type { DashboardSummary, EmailAccountStatus, ResearchStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Overview" };

const ACCOUNT_TONE: Record<EmailAccountStatus, Tone> = {
  connected: "success",
  needs_reauth: "warning",
  error: "danger",
  disconnected: "neutral",
};

export default async function OverviewPage() {
  const s = await serverFetch<DashboardSummary>("/dashboard/summary");

  return (
    <>
      <PageHeader title="Overview" description="Live counts for your workspace." />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="Prospects" value={s.prospects_total} />
        <StatTile label="Drafts awaiting approval" value={s.drafts_awaiting_approval} />
        <StatTile
          label="Emails accepted by provider" value={s.emails_provider_accepted}
          hint="Provider accepted the request. This does not confirm inbox delivery."
        />
        <StatTile label="Replies received" value={s.replies_received} />
        <StatTile label="Active sequences" value={s.active_sequences} />
        <StatTile label="Scheduled actions" value={s.scheduled_actions_pending} />
        <StatTile
          label="Needs attention" value={s.actions_needing_attention}
          hint="Sends with unknown outcome, or failed permanently"
          tone={s.actions_needing_attention > 0 ? "danger" : "neutral"}
        />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card title="Research progress">
          {s.prospects_total === 0 ? (
            <EmptyState title="No prospects yet">
              <Link href="/prospects/import" className="text-accent underline">Import a CSV</Link> to get started. Each
              prospect is researched individually before any draft is written.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border">
              {(Object.keys(RESEARCH_LABELS) as ResearchStatus[]).map((status) => (
                <li key={status} className="flex items-center justify-between py-2 text-sm">
                  <Link href={`/prospects?research_status=${status}`}>
                    <Badge tone={RESEARCH_LABELS[status].tone}>{RESEARCH_LABELS[status].label}</Badge>
                  </Link>
                  <span className="tabular-nums">{s.research_by_status[status] ?? 0}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Email accounts">
          {s.email_accounts.length === 0 ? (
            <EmptyState title="No mailboxes connected">
              Gmail and Microsoft 365 connections (official OAuth) arrive in Milestone 5. Nothing can be sent until a
              mailbox is connected and drafts are approved.
            </EmptyState>
          ) : (
            <ul className="divide-y divide-border">
              {s.email_accounts.map((a) => (
                <li key={a.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                  <div className="min-w-0">
                    <div className="truncate">{a.email_address}</div>
                    <div className="text-xs text-muted">
                      {a.provider === "gmail" ? "Gmail" : "Microsoft 365"} · last sync{" "}
                      {a.last_synced_at ? new Date(a.last_synced_at).toLocaleString() : "never"}
                    </div>
                  </div>
                  <Badge tone={ACCOUNT_TONE[a.status]}>{a.status.replace("_", " ")}</Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
