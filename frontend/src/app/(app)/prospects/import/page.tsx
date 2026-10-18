import type { Metadata } from "next";
import Link from "next/link";

import { Badge, Card, Cell, DataTable, EmptyState, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { formatBytes, IMPORT_STATUS_LABELS } from "@/lib/labels";
import type { ImportSummary } from "@/lib/types";

import { UploadForm } from "./upload-form";

export const metadata: Metadata = { title: "Import prospects" };

export default async function ImportPage() {
  const imports = await serverFetch<ImportSummary[]>("/imports");
  return (
    <>
      <PageHeader
        title="Import prospects"
        description="Upload a CSV, check how its columns map to prospect fields, review every row, then import. Nothing is created until you confirm."
      />
      <div className="grid gap-6 lg:grid-cols-5">
        <Card title="Upload a CSV" className="lg:col-span-2">
          <UploadForm />
          <ul className="mt-4 list-disc space-y-1 pl-5 text-xs text-muted">
            <li>UTF-8 or Windows-1252 text; comma, semicolon, tab, or pipe separated.</li>
            <li>Excel files: use File → Save As → CSV UTF-8 first.</li>
            <li>Every original cell value is kept with the prospect for traceability.</li>
            <li>Missing contact details are never filled in or guessed.</li>
          </ul>
        </Card>
        <div className="lg:col-span-3">
          <h2 className="mb-2 text-sm font-semibold">Recent imports</h2>
          <DataTable
            head={["File", "Rows", "Status", "Uploaded"]}
            empty={imports.length === 0 ? <div className="p-4"><EmptyState title="No imports yet" /></div> : null}
          >
            {imports.map((i) => (
              <tr key={i.id}>
                <Cell>
                  <Link href={`/prospects/imports/${i.id}`} className="font-medium hover:underline">{i.original_filename}</Link>
                  <div className="text-xs text-muted">{formatBytes(i.file_size_bytes)}</div>
                </Cell>
                <Cell className="tabular-nums">
                  {i.row_count}
                  {i.validation_summary.imported !== undefined ? (
                    <div className="text-xs text-muted">{i.validation_summary.imported} imported</div>
                  ) : null}
                </Cell>
                <Cell><Badge tone={IMPORT_STATUS_LABELS[i.status].tone}>{IMPORT_STATUS_LABELS[i.status].label}</Badge></Cell>
                <Cell className="whitespace-nowrap text-muted">{new Date(i.created_at).toLocaleString()}</Cell>
              </tr>
            ))}
          </DataTable>
        </div>
      </div>
    </>
  );
}
