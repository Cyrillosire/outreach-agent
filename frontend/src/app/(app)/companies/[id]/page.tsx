import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { FactList } from "@/components/fact-list";
import { Badge, Card, Cell, DataTable, PageHeader } from "@/components/ui";
import { serverFetchOrNull } from "@/lib/api-server";
import { RESEARCH_LABELS } from "@/lib/labels";
import type { CompanyDetail } from "@/lib/types";

export const metadata: Metadata = { title: "Company" };

export default async function CompanyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const c = await serverFetchOrNull<CompanyDetail>(`/companies/${id}`);
  if (!c) notFound();
  const research = RESEARCH_LABELS[c.research_status];

  return (
    <>
      <PageHeader
        title={c.name}
        description={c.normalized_domain ?? "No domain known. Company research needs a domain."}
        actions={<Badge tone={research.tone}>{research.label}</Badge>}
      />
      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Details">
          <dl className="space-y-2 text-sm">
            <div><dt className="text-muted">Website</dt><dd>
              {c.website ? <a href={c.website} target="_blank" rel="noopener noreferrer" className="text-accent underline">{c.website}</a> : "—"}
            </dd></div>
            <div><dt className="text-muted">Industry</dt><dd>{c.industry ?? "—"}</dd></div>
            <div><dt className="text-muted">Added</dt><dd>{new Date(c.created_at).toLocaleDateString()}</dd></div>
          </dl>
          <p className="mt-4 text-xs text-muted">
            Company research is done once and shared by everyone here. Each person still gets their own research.
          </p>
        </Card>
        <Card title="Research" className="lg:col-span-2">
          <p className="mb-3 text-xs text-muted">
            {c.last_researched_at ? `Last researched ${new Date(c.last_researched_at).toLocaleString()}.` : "Not researched yet."}
          </p>
          <FactList facts={c.facts} emptyTitle="No verified facts yet"
                    emptyHint="Research starts automatically when a prospect from this company is added." />
        </Card>
        <div className="lg:col-span-3">
          <h2 className="mb-2 text-sm font-semibold">People ({c.prospects.length})</h2>
          <DataTable head={["Name", "Title", "Email", "Research"]}>
            {c.prospects.map((p) => (
              <tr key={p.id}>
                <Cell><Link href={`/prospects/${p.id}`} className="font-medium hover:underline">{p.full_name ?? p.email}</Link></Cell>
                <Cell className="text-muted">{p.title ?? "—"}</Cell>
                <Cell className="break-all text-muted">{p.email ?? "—"}</Cell>
                <Cell><Badge tone={RESEARCH_LABELS[p.research_status].tone}>{RESEARCH_LABELS[p.research_status].label}</Badge></Cell>
              </tr>
            ))}
          </DataTable>
        </div>
      </div>
    </>
  );
}
