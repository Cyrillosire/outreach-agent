import type { Metadata } from "next";
import Link from "next/link";

import { Pagination } from "@/components/pagination";
import { Badge, buttonClass, Cell, DataTable, EmptyState, inputClass, PageHeader } from "@/components/ui";
import { serverFetchPage } from "@/lib/api-server";
import { formatDate, OUTREACH_LABELS, RESEARCH_LABELS } from "@/lib/labels";
import { buildQuery, firstParam, pageParam, type SearchParams } from "@/lib/query";
import type { PageMeta, ProspectListItem, ResearchStatus } from "@/lib/types";

export const metadata: Metadata = { title: "Prospects" };

const FILTER_KEYS = ["q", "research_status", "has_email", "suppressed", "import_id", "company_id", "sort"];
const PAGE_SIZE = 50;

export default async function ProspectsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const page = pageParam(params.page);
  const apiQuery = buildQuery(params, [...FILTER_KEYS, "page", "page_size"], {
    page: String(page), page_size: String(PAGE_SIZE),
  });
  const { data: prospects, meta } = await serverFetchPage<ProspectListItem[], PageMeta>(`/prospects${apiQuery}`);
  const filtered = FILTER_KEYS.some((k) => k !== "sort" && firstParam(params[k]));
  const importId = firstParam(params.import_id);

  return (
    <>
      <PageHeader
        title="Prospects"
        description="Everyone you've imported or added. Each prospect is researched individually before any draft is written."
        actions={
          <div className="flex gap-2">
            <Link href="/prospects/new" className={buttonClass("secondary")}>Add prospect</Link>
            <Link href="/prospects/import" className={buttonClass("primary")}>Import CSV</Link>
          </div>
        }
      />

      <form method="get" className="mb-4 grid gap-2 rounded-lg border border-border bg-surface p-3 sm:grid-cols-2 lg:grid-cols-6">
        <input
          name="q" defaultValue={firstParam(params.q) ?? ""} placeholder="Search name, email, title, company…"
          aria-label="Search" className={`${inputClass} lg:col-span-2`}
        />
        <select name="research_status" defaultValue={firstParam(params.research_status) ?? ""}
                aria-label="Research status" className={inputClass}>
          <option value="">Any research status</option>
          {(Object.keys(RESEARCH_LABELS) as ResearchStatus[]).map((s) => (
            <option key={s} value={s}>{RESEARCH_LABELS[s].label}</option>
          ))}
        </select>
        <select name="has_email" defaultValue={firstParam(params.has_email) ?? ""} aria-label="Email" className={inputClass}>
          <option value="">With or without email</option>
          <option value="true">Has email</option>
          <option value="false">No email</option>
        </select>
        <select name="suppressed" defaultValue={firstParam(params.suppressed) ?? ""} aria-label="Suppression"
                className={inputClass}>
          <option value="">Any suppression</option>
          <option value="true">Suppressed</option>
          <option value="false">Not suppressed</option>
        </select>
        <div className="flex gap-2">
          <select name="sort" defaultValue={firstParam(params.sort) ?? "-created"} aria-label="Sort" className={inputClass}>
            <option value="-created">Newest</option>
            <option value="name">Name A–Z</option>
            <option value="company">Company A–Z</option>
            <option value="research">Research status</option>
          </select>
          {importId ? <input type="hidden" name="import_id" value={importId} /> : null}
          <button type="submit" className={buttonClass("primary")}>Apply</button>
        </div>
      </form>

      {importId ? (
        <p className="mb-3 text-sm text-muted">
          Showing prospects from one import. <Link className="text-accent underline" href="/prospects">Show all</Link>
        </p>
      ) : null}

      <DataTable
        head={["Name", "Title", "Company", "Research", "Outreach", "Next action", "Added"]}
        empty={prospects.length === 0 ? (
          <div className="p-4">
            <EmptyState title={filtered ? "No prospects match these filters" : "No prospects yet"}>
              {filtered ? "Try a broader search." : (
                <>Import a CSV or add someone manually.{" "}
                  <Link className="text-accent underline" href="/prospects/import">Import CSV</Link></>
              )}
            </EmptyState>
          </div>
        ) : null}
      >
        {prospects.map((p) => (
          <tr key={p.id} className="hover:bg-surface-muted/60">
            <Cell>
              <Link href={`/prospects/${p.id}`} className="font-medium text-foreground hover:underline">
                {p.full_name ?? p.email ?? "Unnamed prospect"}
              </Link>
              <div className="text-xs text-muted">{p.email ?? <Badge tone="warning">No email</Badge>}</div>
            </Cell>
            <Cell className="text-muted">{p.title ?? "—"}</Cell>
            <Cell>
              {p.company_id ? (
                <Link href={`/companies/${p.company_id}`} className="hover:underline">{p.company_name}</Link>
              ) : <span className="text-muted">—</span>}
              {p.company_domain ? <div className="text-xs text-muted">{p.company_domain}</div> : null}
            </Cell>
            <Cell>
              <Badge tone={RESEARCH_LABELS[p.research_status].tone}>{RESEARCH_LABELS[p.research_status].label}</Badge>
            </Cell>
            <Cell>
              <Badge tone={OUTREACH_LABELS[p.outreach_status].tone}>{OUTREACH_LABELS[p.outreach_status].label}</Badge>
            </Cell>
            <Cell className="whitespace-nowrap text-muted">{p.next_action_at ? formatDate(p.next_action_at) : "—"}</Cell>
            <Cell className="whitespace-nowrap text-muted">{new Date(p.created_at).toLocaleDateString()}</Cell>
          </tr>
        ))}
      </DataTable>
      <Pagination
        page={meta.page} pageSize={meta.page_size} total={meta.total}
        hrefFor={(n) => `/prospects${buildQuery(params, [...FILTER_KEYS, "page"], { page: String(n) })}`}
      />
    </>
  );
}
