"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge, buttonClass, Card, Cell, DataTable, EmptyState, inputClass, Notice, PageHeader, StatTile } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { ApiError, unwrapEnvelope } from "@/lib/envelope";
import { formatBytes, IMPORT_STATUS_LABELS, ISSUE_LABELS, ROW_STATUS_LABELS } from "@/lib/labels";
import type { ImportDetail, ImportRow, ImportRowStatus, PageMeta } from "@/lib/types";

const ROW_FILTERS: { key: string; label: string; status?: ImportRowStatus; withIssues?: boolean }[] = [
  { key: "all", label: "All rows" },
  { key: "valid", label: "Will import", status: "valid" },
  { key: "warnings", label: "With warnings or errors", withIssues: true },
  { key: "invalid", label: "Invalid", status: "invalid" },
  { key: "duplicate_in_file", label: "Duplicates", status: "duplicate_in_file" },
  { key: "already_exists", label: "Already exist", status: "already_exists" },
  { key: "imported", label: "Imported", status: "imported" },
];
const PAGE_SIZE = 25;

function describeError(err: unknown): string {
  return err instanceof ApiError ? err.message : "Something went wrong. Check your connection and try again.";
}

export function ImportWorkspace({ initial }: { initial: ImportDetail }) {
  const router = useRouter();
  const [imp, setImp] = useState(initial);
  const [mapping, setMapping] = useState<Record<string, string>>(initial.column_mapping);
  const [busy, setBusy] = useState<null | "validate" | "commit" | "cancel">(null);
  const [error, setError] = useState<string | null>(null);

  const editable = imp.status === "uploaded" || imp.status === "validated";
  const dirty = useMemo(
    () => imp.headers.some((h) => (mapping[h] ?? "ignore") !== (imp.column_mapping[h] ?? "ignore")),
    [mapping, imp],
  );
  const needsValidation = imp.status === "uploaded" || dirty;
  const s = imp.validation_summary;
  const statusLabel = IMPORT_STATUS_LABELS[imp.status];

  async function run(kind: "validate" | "commit" | "cancel", fn: () => Promise<ImportDetail>) {
    setBusy(kind);
    setError(null);
    try {
      const next = await fn();
      setImp(next);
      setMapping(next.column_mapping);
      router.refresh();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(null);
    }
  }

  const validate = () => run("validate", () => apiFetch<ImportDetail>(`/imports/${imp.id}/mapping`, {
    method: "PUT", body: JSON.stringify({ mapping }),
  }));
  const commit = () => {
    if (!window.confirm(`Create ${s.valid ?? 0} prospect(s) from this file?`)) return;
    void run("commit", () => apiFetch<ImportDetail>(`/imports/${imp.id}/commit`, { method: "POST" }));
  };
  const cancel = () => {
    if (!window.confirm("Discard this import? Nothing has been imported from it.")) return;
    void run("cancel", () => apiFetch<ImportDetail>(`/imports/${imp.id}/cancel`, { method: "POST" }));
  };

  return (
    <>
      <PageHeader
        title={imp.original_filename}
        description={`${imp.row_count} data rows · ${imp.headers.length} columns · ${formatBytes(imp.file_size_bytes)} · ${imp.encoding}, "${imp.delimiter === "\t" ? "tab" : imp.delimiter}" separated`}
        actions={<Badge tone={statusLabel.tone}>{statusLabel.label}</Badge>}
      />
      <p className="-mt-4 mb-6 text-sm"><Link href="/prospects/import" className="text-accent underline">← All imports</Link></p>

      {error ? <div className="mb-4"><Notice tone="danger" title="That didn't work">{error}</Notice></div> : null}

      {imp.status === "completed" ? (
        <div className="mb-6 space-y-4">
          <Notice tone="success" title={`Imported ${s.imported ?? 0} prospect(s)`}>
            {s.companies_created ?? 0} new compan{s.companies_created === 1 ? "y" : "ies"} created,{" "}
            {s.companies_reused ?? 0} existing reused. Research will start automatically once the research engine
            (Milestone 3) is enabled; nothing is emailed without your approval.
          </Notice>
          <Link href={`/prospects?import_id=${imp.id}`} className={buttonClass("primary")}>View imported prospects</Link>
        </div>
      ) : null}
      {imp.status === "cancelled" ? (
        <div className="mb-6"><Notice tone="neutral" title="This import was cancelled">No prospects were created from it.</Notice></div>
      ) : null}

      <Card title="1. Map columns" className="mb-6">
        <p className="mb-3 text-sm text-muted">
          Choose what each column contains. Columns set to <em>Ignore</em> aren&apos;t used, but their original values are
          still kept with each row. <em>Custom attribute</em> stores the value on the prospect under the column name.
        </p>
        <DataTable head={["Column", "Sample values", "Maps to"]}>
          {imp.headers.map((header) => {
            const samples = imp.sample_rows.map((r) => r[header]).filter((v) => v && v.trim()).slice(0, 3);
            return (
              <tr key={header}>
                <Cell className="font-medium">{header}</Cell>
                <Cell className="max-w-xs text-muted">
                  {samples.length ? samples.map((v, i) => <div key={i} className="truncate" title={v}>{v}</div>) : <span>(empty)</span>}
                </Cell>
                <Cell>
                  <select
                    aria-label={`Field for column ${header}`} disabled={!editable || busy !== null}
                    value={mapping[header] ?? "ignore"}
                    onChange={(e) => setMapping({ ...mapping, [header]: e.target.value })}
                    className={`${inputClass} min-w-48`}
                  >
                    {imp.targets.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </Cell>
              </tr>
            );
          })}
        </DataTable>
        {editable ? (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button type="button" onClick={validate} disabled={busy !== null || !needsValidation} className={buttonClass("primary")}>
              {busy === "validate" ? "Validating…" : imp.status === "uploaded" ? `Validate ${imp.row_count} rows` : "Re-validate with these changes"}
            </button>
            {!needsValidation ? <span className="text-sm text-muted">Mapping applied. Change a column to re-validate.</span> : null}
          </div>
        ) : null}
      </Card>

      {imp.status !== "uploaded" && s.total_rows !== undefined ? (
        <Card title="2. Review" className="mb-6">
          {dirty ? (
            <div className="mb-4"><Notice tone="warning" title="Mapping changed">Re-validate before importing; these results reflect the previous mapping.</Notice></div>
          ) : null}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label={imp.status === "completed" ? "Imported" : "Will import"} value={imp.status === "completed" ? s.imported ?? 0 : s.valid ?? 0}
                      hint={s.with_warnings ? `${s.with_warnings} with warnings` : undefined} />
            <StatTile label="Invalid (skipped)" value={s.invalid ?? 0} tone={(s.invalid ?? 0) > 0 ? "danger" : "neutral"} />
            <StatTile label="Duplicates in file" value={s.duplicate_in_file ?? 0} hint="First occurrence is kept" />
            <StatTile label="Already exist" value={s.already_exists ?? 0} hint="Never overwritten" />
            <StatTile label="Without email" value={s.without_email ?? 0} hint="Researchable, not emailable" />
            <StatTile label="New companies" value={s.companies_new ?? 0} />
            <StatTile label="Existing companies" value={s.companies_existing ?? 0} hint="Research is reused" />
            <StatTile label="Blank rows skipped" value={s.blank_rows_skipped ?? 0} />
          </div>
          {s.issue_counts && Object.keys(s.issue_counts).length > 0 ? (
            <div className="mt-4">
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">Issues found</h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(s.issue_counts).map(([code, n]) => (
                  <Badge key={code} tone="neutral">{ISSUE_LABELS[code] ?? code}: {n}</Badge>
                ))}
              </div>
            </div>
          ) : null}
          <RowPreview importId={imp.id} version={`${imp.status}:${JSON.stringify(imp.validation_summary)}`} mapping={imp.column_mapping} />
        </Card>
      ) : null}

      {imp.status === "validated" ? (
        <Card title="3. Import">
          <p className="mb-4 text-sm text-muted">
            Creates {s.valid ?? 0} prospect(s) and links them to companies by domain. Invalid rows, duplicates, and existing
            prospects are skipped. Importing doesn&apos;t send anything.
          </p>
          <div className="flex flex-wrap gap-3">
            <button type="button" onClick={commit} disabled={busy !== null || dirty || (s.valid ?? 0) === 0} className={buttonClass("primary")}>
              {busy === "commit" ? "Importing…" : `Import ${s.valid ?? 0} prospect(s)`}
            </button>
            <button type="button" onClick={cancel} disabled={busy !== null} className={buttonClass("secondary")}>
              {busy === "cancel" ? "Cancelling…" : "Discard import"}
            </button>
          </div>
          {(s.valid ?? 0) === 0 ? <p className="mt-2 text-sm text-warning">No rows are importable with the current mapping.</p> : null}
          {dirty ? <p className="mt-2 text-sm text-warning">Re-validate your mapping changes before importing.</p> : null}
        </Card>
      ) : null}
    </>
  );
}

function pick(row: ImportRow, mapping: Record<string, string>, target: string): string | null {
  const normalized = row.normalized?.[target];
  if (typeof normalized === "string" && normalized) return normalized;
  const header = Object.keys(mapping).find((h) => mapping[h] === target);
  const raw = header ? row.raw[header]?.trim() : "";
  return raw || null;
}

function RowPreview({ importId, version, mapping }: { importId: string; version: string; mapping: Record<string, string> }) {
  const [filter, setFilter] = useState("all");
  const [page, setPage] = useState(1);
  // The last completed load, tagged with the request it answers. State is only set from the fetch callback,
  // and a response for an outdated request (filter/page changed meanwhile) is discarded.
  const [loaded, setLoaded] = useState<{ key: string; rows: ImportRow[]; meta: PageMeta; error: string | null } | null>(null);
  const requestKey = `${importId}|${filter}|${page}|${version}`;
  const loading = loaded?.key !== requestKey;
  const rows = loaded?.rows ?? [];
  const meta = loaded?.meta ?? { page: 1, page_size: PAGE_SIZE, total: 0 };
  const error = loading ? null : loaded?.error ?? null;

  const fetchRows = useCallback(async (): Promise<{ rows: ImportRow[]; meta: PageMeta; error: string | null }> => {
    const f = ROW_FILTERS.find((x) => x.key === filter) ?? ROW_FILTERS[0];
    const query = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (f.status) query.set("status", f.status);
    if (f.withIssues) query.set("with_issues", "true");
    try {
      const res = await fetch(`/api/v1/imports/${importId}/rows?${query}`, { credentials: "same-origin" });
      const { data, meta: m } = await unwrapEnvelope<ImportRow[], PageMeta>(res);
      return { rows: data, meta: m, error: null };
    } catch (err) {
      return { rows: [], meta: { page, page_size: PAGE_SIZE, total: 0 }, error: describeError(err) };
    }
  }, [importId, filter, page]);

  useEffect(() => {
    let current = true;
    fetchRows().then((result) => {
      if (current) setLoaded({ key: requestKey, ...result });
    });
    return () => { current = false; };
  }, [fetchRows, requestKey]);

  const pages = Math.max(1, Math.ceil(meta.total / PAGE_SIZE));
  return (
    <div className="mt-6">
      <div role="tablist" aria-label="Filter rows" className="mb-3 flex flex-wrap gap-1">
        {ROW_FILTERS.map((f) => (
          <button
            key={f.key} type="button" role="tab" aria-selected={filter === f.key}
            onClick={() => { setFilter(f.key); setPage(1); }}
            className={`rounded-md px-2.5 py-1 text-xs font-medium ${filter === f.key ? "bg-accent text-accent-foreground" : "bg-surface-muted text-muted hover:text-foreground"}`}
          >
            {f.label}
          </button>
        ))}
      </div>
      {error ? <Notice tone="danger" title="Couldn't load rows">{error}</Notice> : null}
      <DataTable
        head={["Row", "Name", "Email", "Company", "Status", "Notes"]}
        empty={!loading && rows.length === 0 ? <div className="p-4"><EmptyState title="No rows in this view" /></div> : null}
      >
        {rows.map((row) => (
          <tr key={row.row_number} className={loading ? "opacity-50" : ""}>
            <Cell className="tabular-nums text-muted">{row.row_number}</Cell>
            <Cell>{pick(row, mapping, "full_name") ?? <span className="text-muted">—</span>}</Cell>
            <Cell className="break-all">{pick(row, mapping, "email") ?? <span className="text-muted">—</span>}</Cell>
            <Cell>
              {pick(row, mapping, "company_name") ?? <span className="text-muted">—</span>}
              {typeof row.normalized?.company_domain === "string" ? (
                <div className="text-xs text-muted">{row.normalized.company_domain}</div>
              ) : null}
            </Cell>
            <Cell>
              {row.status ? <Badge tone={ROW_STATUS_LABELS[row.status].tone}>{ROW_STATUS_LABELS[row.status].label}</Badge> : null}
              {row.prospect_id ? <div className="mt-1 text-xs"><Link href={`/prospects/${row.prospect_id}`} className="text-accent underline">View</Link></div> : null}
              {row.existing_prospect_id ? <div className="mt-1 text-xs"><Link href={`/prospects/${row.existing_prospect_id}`} className="text-accent underline">Existing prospect</Link></div> : null}
            </Cell>
            <Cell className="max-w-md">
              <ul className="space-y-0.5">
                {row.issues.map((i, n) => (
                  <li key={n} className={`text-xs ${i.severity === "error" ? "text-danger" : "text-muted"}`}>{i.message}</li>
                ))}
              </ul>
            </Cell>
          </tr>
        ))}
      </DataTable>
      {meta.total > PAGE_SIZE ? (
        <div className="mt-3 flex items-center justify-between text-sm text-muted">
          <span>Page {page} of {pages} · {meta.total} rows</span>
          <div className="flex gap-2">
            <button type="button" className={buttonClass()} disabled={page <= 1 || loading} onClick={() => setPage(page - 1)}>Previous</button>
            <button type="button" className={buttonClass()} disabled={page >= pages || loading} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
