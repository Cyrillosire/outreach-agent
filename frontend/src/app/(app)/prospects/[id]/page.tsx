import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { FactList } from "@/components/fact-list";
import { ResearchActions } from "@/components/research-actions";
import { Badge, Card, EmptyState, Notice, PageHeader } from "@/components/ui";
import { serverFetchOrNull } from "@/lib/api-server";
import { formatDate, RESEARCH_LABELS } from "@/lib/labels";
import type { LinkedInCapture, ProspectDetail, ProspectResearch } from "@/lib/types";

import { LinkedInCaptures } from "./linkedin-captures";
import { DeleteProspectButton, EditProspect, SuppressForm } from "./prospect-actions";

export const metadata: Metadata = { title: "Prospect" };

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-3 gap-3 py-2 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="col-span-2 break-words">{children ?? <span className="text-muted">—</span>}</dd>
    </div>
  );
}

export default async function ProspectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const [p, r, captures] = await Promise.all([
    serverFetchOrNull<ProspectDetail>(`/prospects/${id}`),
    serverFetchOrNull<ProspectResearch>(`/prospects/${id}/research`),
    serverFetchOrNull<LinkedInCapture[]>(`/prospects/${id}/linkedin-captures`),
  ]);
  if (!p || !r) notFound();

  const research = RESEARCH_LABELS[p.research_status];
  const missing = [
    ...(r.individual_run?.sufficiency?.missing ?? []).map((m) => `${p.full_name ?? "Individual"}: ${m}`),
    ...(r.company?.latest_run?.sufficiency?.missing ?? []).map((m) => `${r.company?.name}: ${m}`),
  ];
  const notes = [...new Set([...(r.individual_run?.sufficiency?.notes ?? []), ...(r.company?.latest_run?.sufficiency?.notes ?? []),
    ...(r.individual_run?.error?.message ? [r.individual_run.error.message] : [])])];
  const emailDomain = p.email_normalized?.split("@")[1] ?? null;
  const domain = p.company?.normalized_domain ?? emailDomain;
  const subtitle = [p.title, p.company?.name].filter(Boolean).join(" at ");

  return (
    <>
      <PageHeader
        title={p.full_name ?? p.email ?? "Unnamed prospect"}
        description={subtitle || undefined}
        actions={
          <div className="flex items-center gap-2">
            <Badge tone={research.tone}>{research.label}</Badge>
            {p.suppressions.length ? <Badge tone="danger">Suppressed</Badge> : null}
            <EditProspect prospectId={p.id} initial={{
              full_name: p.full_name ?? "", email: p.email ?? "", first_name: p.first_name ?? "",
              last_name: p.last_name ?? "", title: p.title ?? "", linkedin_url: p.linkedin_url ?? "",
              company_name: p.company?.name ?? "", company_domain: p.company?.normalized_domain ?? "",
              company_website: p.company?.website ?? "", industry: p.industry ?? "", country: p.country ?? "",
              location: p.location ?? "",
            }} />
            <DeleteProspectButton prospectId={p.id} name={p.full_name ?? p.email ?? "this contact"} />
          </div>
        }
      />
      <p className="-mt-4 mb-6 text-sm"><Link href="/prospects" className="text-accent underline">← Prospects</Link></p>

      {p.blockers.length ? (
        <div className="mb-6">
          <Notice tone="warning" title="Why this prospect can't be emailed yet">
            <ul className="mt-1 list-disc pl-5">{p.blockers.map((b) => <li key={b.code + b.message}>{b.message}</li>)}</ul>
          </Notice>
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Profile">
            <dl className="divide-y divide-border">
              <Row label="Email">
                {p.email ? <>{p.email} <Badge>{p.email_verification_status.replace("_", " ")}</Badge></> : null}
              </Row>
              <Row label="Title">{p.title}</Row>
              <Row label="LinkedIn">
                {p.linkedin_url ? <a href={p.linkedin_url} target="_blank" rel="noopener noreferrer" className="text-accent underline">{p.linkedin_url}</a> : null}
              </Row>
              <Row label="Location">{p.location}</Row>
              <Row label="Country / region">{p.country}</Row>
              <Row label="Industry">{p.industry}</Row>
              {Object.entries(p.attributes).map(([k, v]) => <Row key={k} label={k.replaceAll("_", " ")}>{v}</Row>)}
            </dl>
          </Card>

          <Card title="Research">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
              <div>
                <Badge tone={research.tone}>{research.label}</Badge>
                {r.outcome_reason ? <p className="mt-2 text-sm text-muted">{r.outcome_reason}</p> : null}
              </div>
              <ResearchActions prospectId={p.id} phase={r.phase} queuePosition={r.queue_position}
                               companyName={r.company?.name ?? null}
                               hasResearch={Boolean(r.individual_run || r.company?.latest_run)} />
            </div>
            {missing.length ? (
              <div className="mb-4">
                <Notice tone="info" title="Not found in public sources">
                  <ul className="list-disc pl-5">{missing.map((m) => <li key={m}>{m}</li>)}</ul>
                </Notice>
              </div>
            ) : null}
            {notes.length ? (
              <ul className="mb-4 space-y-1 text-xs text-muted">{notes.map((n) => <li key={n}>{n}</li>)}</ul>
            ) : null}
            <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">About {p.full_name ?? "this person"}</h3>
            <FactList
              facts={r.individual_facts}
              emptyTitle={r.in_progress ? "Researching…" : "No verified facts about this person"}
              emptyHint={r.in_progress ? undefined : "Nothing about this individual could be confirmed from public sources."}
            />
            {r.company ? (
              <>
                <h3 className="mb-1 mt-6 text-xs font-medium uppercase tracking-wide text-muted">About {r.company.name}</h3>
                <FactList
                  facts={r.company.facts}
                  emptyTitle={r.in_progress ? "Researching…" : "No verified facts about this company"}
                />
              </>
            ) : null}
          </Card>

          <LinkedInCaptures prospectId={p.id} linkedinUrl={p.linkedin_url} captures={captures} />

          <Card title="Drafts, messages and scheduled actions">
            {p.drafts.length + p.messages.length + p.scheduled_actions.length === 0 ? (
              <EmptyState title="Nothing yet">
                Drafts (Milestone 4) and sending, replies, and follow-ups (Milestones 5–6) will appear here.
              </EmptyState>
            ) : (
              <ul className="space-y-1 text-sm">
                {p.drafts.map((d) => (
                  <li key={d.id}>
                    <Link href={`/drafts/${d.id}`} className="hover:underline">Draft v{d.version}: {d.subject}</Link>{" "}
                    <Badge>{d.status.replace("_", " ")}</Badge>
                  </li>
                ))}
                {p.messages.map((m) => <li key={m.id}>{m.direction} message: {m.subject ?? "(no subject)"}</li>)}
                {p.scheduled_actions.map((a) => <li key={a.id}>{a.action_type} due {formatDate(a.due_at)} <Badge>{a.state}</Badge></li>)}
              </ul>
            )}
          </Card>

          <Card title="Source">
            <dl className="divide-y divide-border">
              <Row label="Added via">{p.provenance.source === "csv_import" ? "CSV import" : "Manual entry"}</Row>
              {p.provenance.import_id ? (
                <Row label="File">
                  <Link href={`/prospects/imports/${p.provenance.import_id}`} className="text-accent underline">
                    {p.provenance.import_filename}
                  </Link>
                  {p.provenance.row_number ? `, row ${p.provenance.row_number}` : null}
                </Row>
              ) : null}
              <Row label="Added">{formatDate(p.created_at)}</Row>
            </dl>
            {p.provenance.raw ? (
              <details className="mt-3">
                <summary className="cursor-pointer text-sm text-accent">Original row values</summary>
                <dl className="mt-2 divide-y divide-border rounded-md border border-border px-3">
                  {Object.entries(p.provenance.raw).map(([k, v]) => <Row key={k} label={k}>{v || null}</Row>)}
                </dl>
              </details>
            ) : null}
          </Card>
        </div>

        <div className="space-y-6">
          <Card title="Company">
            {p.company ? (
              <dl className="divide-y divide-border">
                <Row label="Name"><Link href={`/companies/${p.company.id}`} className="text-accent underline">{p.company.name}</Link></Row>
                <Row label="Domain">{p.company.normalized_domain}</Row>
                <Row label="Research"><Badge tone={RESEARCH_LABELS[p.company.research_status].tone}>{RESEARCH_LABELS[p.company.research_status].label}</Badge></Row>
              </dl>
            ) : <EmptyState title="No company linked">Add a company name or domain to enable company research.</EmptyState>}
          </Card>

          <Card title="Suppression status">
            {p.suppressions.length ? (
              <ul className="space-y-2 text-sm">
                {p.suppressions.map((s) => (
                  <li key={s.id} className="rounded-md bg-danger-bg px-3 py-2 text-danger">
                    <div className="font-medium">{s.reason} · {s.scope === "workspace" ? "all campaigns" : "one campaign"}</div>
                    <div className="text-xs">{s.email_normalized ?? `domain ${s.domain}`} · since {formatDate(s.created_at)}</div>
                    {s.note ? <div className="mt-1 text-xs">{s.note}</div> : null}
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-muted">Not suppressed.</p>}
          </Card>

          <SuppressForm prospectId={p.id} hasEmail={Boolean(p.email_normalized)} domain={domain} />
        </div>
      </div>
    </>
  );
}
