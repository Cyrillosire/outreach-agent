import type { Metadata } from "next";
import Link from "next/link";

import { Badge, buttonClass, Card, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import type { SystemStatus, User } from "@/lib/types";

export const metadata: Metadata = { title: "Settings" };

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-2 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="text-right">{children}</dd>
    </div>
  );
}

function Health({ ok, okText = "OK", failText = "Unavailable" }: { ok: boolean; okText?: string; failText?: string }) {
  return <Badge tone={ok ? "success" : "danger"}>{ok ? okText : failText}</Badge>;
}

export default async function SettingsPage() {
  const [user, status] = await Promise.all([
    serverFetch<User>("/auth/me"),
    serverFetch<SystemStatus>("/system/status"),
  ]);

  return (
    <>
      <PageHeader title="Settings" description="Your account and the platform's operational status." />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Your account">
          <dl className="divide-y divide-border">
            <Row label="Name">{user.display_name}</Row>
            <Row label="Email">{user.email}</Row>
            <Row label="Role"><Badge>{user.role}</Badge></Row>
          </dl>
        </Card>

        <Card title="System status">
          <dl className="divide-y divide-border">
            <Row label="Environment"><Badge tone="info">{status.environment}</Badge></Row>
            <Row label="Database"><Health ok={status.database} /></Row>
            <Row label="Redis"><Health ok={status.redis} /></Row>
            <Row label="Schema">
              <Health ok={status.schema_up_to_date} okText={`Up to date (${status.schema_revision})`}
                      failText={`Pending migration (${status.schema_revision ?? "none"} → ${status.schema_head})`} />
            </Row>
            <Row label="Credential encryption">
              <Health ok={status.credential_encryption_configured} okText="Configured" failText="Not configured" />
            </Row>
          </dl>
        </Card>

        <Card title="Mailboxes">
          <p className="text-sm text-muted">Gmail and Microsoft 365 accounts you send from, their status, and test sends.</p>
          <Link href="/settings/mailboxes" className={`${buttonClass("primary")} mt-3`}>Manage mailboxes</Link>
        </Card>

        <Card title="Coming in later milestones">
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
            <li>Reply detection, follow-up scheduling, and unsubscribe handling (Milestone 6)</li>
            <li>Data retention policies, export and deletion (Milestone 7)</li>
          </ul>
        </Card>
      </div>
    </>
  );
}
