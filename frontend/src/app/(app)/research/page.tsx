import type { Metadata } from "next";
import Link from "next/link";

import { RetryFailedButton } from "@/components/research-actions";
import { Badge, Card, Cell, DataTable, EmptyState, Notice, PageHeader, StatTile, type Tone } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { formatDate, RESEARCH_LABELS } from "@/lib/labels";
import type { ResearchOverview, ResearchStatus, RunStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Research" };

const RUN_TONE: Record<RunStatus, Tone> = {
  queued: "neutral", running: "info", succeeded: "success", insufficient_evidence: "warning",
  failed_retryable: "warning", failed: "danger", cancelled: "neutral",
};

export default async function ResearchPage() {
  const o = await serverFetch<ResearchOverview>("/research/overview");
  const s = o.prospects_by_status;
  const inProgress = s.pending + s.researching_company + s.researching_individual + s.verifying;
  const searchReady = o.providers.tavily || o.providers.brave;

  return (
    <>
      <PageHeader
        title="Research"
        description="Every prospect and their company are researched from public sources. Each fact is stored with a verbatim excerpt and a link to its source."
        actions={<RetryFailedButton count={s.failed_retryable} />}
      />

      {!o.providers.groq ? (
        <div className="mb-4">
          <Notice tone="warning" title="Groq API key not set">
            Research runs only on the local model, which is private but takes minutes per page on this laptop. Add{" "}
            <code>GROQ_API_KEY</code> to <code>.env</code> (see <code>docs/provider-setup.md</code>) and restart the
            worker.
          </Notice>
        </div>
      ) : null}
      {!searchReady ? (
        <div className="mb-4">
          <Notice tone="warning" title="No web search provider configured">
            Company research uses each company&apos;s own website only, and individuals can only be found on it. Add{" "}
            <code>TAVILY_API_KEY</code> and/or <code>BRAVE_API_KEY</code> for independent sources and corroboration.
          </Notice>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="In progress" value={inProgress} hint={`${o.jobs_by_state.queued} queued · ${o.jobs_by_state.running} running`} />
        <StatTile label="Ready for personalization" value={s.ready_for_personalization} />
        <StatTile label="Needs review" value={s.needs_review} hint="Thin, one-sided, or conflicting evidence" />
        <StatTile label="Insufficient / failed" value={s.insufficient_evidence + s.failed_retryable}
                  tone={s.failed_retryable > 0 ? "danger" : "neutral"}
                  hint={s.failed_retryable ? `${s.failed_retryable} failed and can be retried` : undefined} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <Card title="Prospects by research status" className="lg:col-span-1">
          <ul className="divide-y divide-border">
            {(Object.keys(RESEARCH_LABELS) as ResearchStatus[]).map((status) => (
              <li key={status} className="flex items-center justify-between py-2 text-sm">
                <Link href={`/prospects?research_status=${status}`}>
                  <Badge tone={RESEARCH_LABELS[status].tone}>{RESEARCH_LABELS[status].label}</Badge>
                </Link>
                <span className="tabular-nums">{s[status] ?? 0}</span>
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Providers" className="lg:col-span-2">
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            {(["groq", "ollama", "tavily", "brave"] as const).map((p) => (
              <div key={p} className="flex items-center justify-between rounded-md bg-surface-muted px-3 py-2">
                <dt className="capitalize">{p === "ollama" ? "Ollama (local)" : p}</dt>
                <dd><Badge tone={o.providers[p] ? "success" : "neutral"}>{o.providers[p] ? "Configured" : "Not set"}</Badge></dd>
              </div>
            ))}
          </dl>
          <p className="mt-3 text-xs text-muted">
            Extraction models, in fallback order: {o.llm_extraction_chain.join(" → ")}. Search order:{" "}
            {o.search_order.join(", ")}. Automatic research for new prospects is {o.auto_start ? "on" : "off"}.
          </p>
        </Card>
      </div>

      <h2 className="mb-2 mt-8 text-sm font-semibold">Recent research runs</h2>
      <DataTable
        head={["Subject", "Type", "Status", "Stage", "Attempts", "Started", "Finished", "Notes"]}
        empty={o.recent_runs.length === 0 ? (
          <div className="p-4"><EmptyState title="No research has run yet">Import prospects to start research automatically.</EmptyState></div>
        ) : null}
      >
        {o.recent_runs.map((r) => (
          <tr key={r.id}>
            <Cell>
              {r.prospect_id ? <Link className="hover:underline" href={`/prospects/${r.prospect_id}`}>{r.subject_name ?? "Prospect"}</Link>
                : r.company_id ? <Link className="hover:underline" href={`/companies/${r.company_id}`}>{r.subject_name ?? "Company"}</Link>
                  : r.subject_name}
            </Cell>
            <Cell className="text-muted">{r.subject_type === "company" ? "Company" : "Individual"}</Cell>
            <Cell><Badge tone={RUN_TONE[r.status]}>{r.status.replaceAll("_", " ")}</Badge></Cell>
            <Cell className="text-muted">{r.stage ?? "—"}</Cell>
            <Cell className="tabular-nums">{r.attempt_count}</Cell>
            <Cell className="whitespace-nowrap text-muted">{formatDate(r.started_at)}</Cell>
            <Cell className="whitespace-nowrap text-muted">{formatDate(r.finished_at)}</Cell>
            <Cell className="max-w-sm text-xs text-muted">
              {r.error?.message ?? r.sufficiency?.notes?.join(" ") ?? ""}
            </Cell>
          </tr>
        ))}
      </DataTable>
    </>
  );
}
