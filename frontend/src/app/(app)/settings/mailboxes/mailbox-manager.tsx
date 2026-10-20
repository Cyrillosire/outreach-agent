"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";

import { Modal } from "@/components/modal";
import { Badge, buttonClass, Card, Cell, DataTable, EmptyState, Notice } from "@/components/ui";
import { apiFetch } from "@/lib/api-client";
import { errorMessage } from "@/lib/errors";
import { formatDate, MAILBOX_STATUS_LABELS, OUTBOUND_LABELS, PROVIDER_LABELS } from "@/lib/labels";
import type { Mailbox, MailProvider, MailProvidersInfo, OutboundMessage } from "@/lib/types";

const noSubscribe = () => () => {};

export function MailboxManager({ info, mailboxes }: { info: MailProvidersInfo; mailboxes: Mailbox[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "danger" | "warning" | "info"; text: string } | null>(null);
  const [confirm, setConfirm] = useState<Mailbox | null>(null);
  const [testing, setTesting] = useState<OutboundMessage | null>(null);
  // The browser's origin (null during server render): connecting only works from APP_PUBLIC_URL's origin.
  const origin = useSyncExternalStore(noSubscribe, () => window.location.origin, () => null);
  const publicOrigin = new URL(info.app_public_url).origin;
  const wrongOrigin = origin !== null && origin !== publicOrigin;

  // Poll a test send until it settles.
  useEffect(() => {
    if (!testing || !["queued", "sending"].includes(testing.status ?? "")) return;
    const t = window.setTimeout(async () => {
      try {
        setTesting(await apiFetch<OutboundMessage>(`/email-messages/${testing.id}`));
      } catch {
        // keep the last known state
      }
    }, 2000);
    return () => window.clearTimeout(t);
  }, [testing]);

  async function connect(provider: MailProvider) {
    setBusy(provider);
    setNotice(null);
    try {
      const r = await apiFetch<{ authorization_url: string }>(`/mailboxes/oauth/${provider}/start`, {
        method: "POST", body: JSON.stringify({}),
      });
      window.location.assign(r.authorization_url);
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't start the connection.") });
      setBusy(null);
    }
  }

  async function testSend(m: Mailbox) {
    setBusy(m.id);
    setNotice(null);
    try {
      setTesting(await apiFetch<OutboundMessage>(`/mailboxes/${m.id}/test-send`, { method: "POST" }));
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't send the test email.") });
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(m: Mailbox) {
    setBusy(m.id);
    try {
      const r = await apiFetch<{ provider_revoked: boolean }>(`/mailboxes/${m.id}`, { method: "DELETE" });
      setNotice(r.provider_revoked
        ? { tone: "success", text: `Disconnected ${m.email_address} and revoked the app's access.` }
        : { tone: "warning", text: `Disconnected ${m.email_address}. Microsoft doesn't allow apps to revoke their own access: remove "Outreach Agent" at myaccount.microsoft.com → Apps to finish.` });
      setConfirm(null);
      router.refresh();
    } catch (err) {
      setNotice({ tone: "danger", text: errorMessage(err, "Couldn't disconnect.") });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-6">
      {notice ? <Notice tone={notice.tone} title={notice.text} /> : null}
      {!info.encryption_configured ? (
        <Notice tone="danger" title="Credential encryption isn't configured">
          Set CREDENTIAL_ENCRYPTION_KEYS in .env before connecting mailboxes; tokens are never stored unencrypted.
        </Notice>
      ) : null}
      {wrongOrigin ? (
        <Notice tone="warning" title={`Open the app at ${publicOrigin} to connect a mailbox`}>
          The provider sends you back to {publicOrigin}, and your login must be on that address.{" "}
          <a className="underline" href={`${publicOrigin}/settings/mailboxes`}>Switch to {publicOrigin}</a> and log in there.
        </Notice>
      ) : null}

      <Card title="Connected mailboxes">
        {mailboxes.length === 0 ? (
          <EmptyState title="No mailboxes yet">Connect the account you&apos;ll send outreach from.</EmptyState>
        ) : (
          <DataTable head={["Mailbox", "Provider", "Status", "Sent (24h)", "Connected", ""]}>
            {mailboxes.map((m) => {
              const status = MAILBOX_STATUS_LABELS[m.status];
              return (
                <tr key={m.id}>
                  <Cell>
                    <div className="font-medium">{m.email_address}</div>
                    {m.display_name ? <div className="text-xs text-muted">{m.display_name}</div> : null}
                    {m.last_error && m.status !== "connected" ? <div className="mt-1 max-w-sm text-xs text-danger">{m.last_error}</div> : null}
                  </Cell>
                  <Cell className="text-muted">{PROVIDER_LABELS[m.provider]}</Cell>
                  <Cell><Badge tone={status.tone}>{status.label}</Badge></Cell>
                  <Cell className="tabular-nums">{m.sent_last_24h} / {info.daily_send_limit}</Cell>
                  <Cell className="whitespace-nowrap text-muted">{formatDate(m.connected_at)}</Cell>
                  <Cell className="whitespace-nowrap text-right">
                    {m.status === "connected" ? (
                      <button type="button" className={buttonClass()} disabled={busy !== null} onClick={() => testSend(m)}>
                        Send test email
                      </button>
                    ) : m.status !== "disconnected" ? (
                      <button type="button" className={buttonClass("primary")} disabled={busy !== null || wrongOrigin}
                              onClick={() => connect(m.provider)}>Reconnect</button>
                    ) : null}
                    {m.status !== "disconnected" ? (
                      <button type="button" className={`${buttonClass("ghost")} ml-2`} onClick={() => setConfirm(m)}>Disconnect</button>
                    ) : null}
                  </Cell>
                </tr>
              );
            })}
          </DataTable>
        )}
        {testing ? (
          <div className="mt-4">
            <Notice tone={testing.status ? OUTBOUND_LABELS[testing.status].tone : "info"}
                    title={`Test email to ${testing.to_addresses[0]}: ${testing.status ? OUTBOUND_LABELS[testing.status].label : "…"}`}>
              {testing.status === "provider_accepted" ? "Check that inbox; it should arrive within a minute." : null}
              {testing.last_error ?? (testing.status ? OUTBOUND_LABELS[testing.status].hint : null)}
            </Notice>
          </div>
        ) : null}
      </Card>

      <Card title="Connect a mailbox">
        <div className="grid gap-4 md:grid-cols-2">
          {info.providers.map((p) => (
            <div key={p.provider} className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between">
                <div className="font-medium">{PROVIDER_LABELS[p.provider]}</div>
                {p.configured ? <Badge tone="success">Ready</Badge> : <Badge tone="warning">Not configured</Badge>}
              </div>
              {p.configured ? (
                <button type="button" className={`${buttonClass("primary")} mt-3`}
                        disabled={busy !== null || wrongOrigin || !info.encryption_configured}
                        onClick={() => connect(p.provider)}>
                  {busy === p.provider ? "Opening…" : `Connect ${p.provider === "gmail" ? "Gmail" : "Microsoft"}`}
                </button>
              ) : (
                <p className="mt-2 text-sm text-muted">
                  Add its OAuth client id and secret to .env (see docs/provider-setup.md), then restart the app.
                </p>
              )}
              <details className="mt-3 text-xs text-muted">
                <summary className="cursor-pointer">Setup details</summary>
                <p className="mt-2">Redirect URI to register:</p>
                <code className="block break-all rounded bg-surface-muted p-2">{p.redirect_uri}</code>
                <p className="mt-2">Permissions requested: {p.scopes.join(", ")}</p>
              </details>
            </div>
          ))}
        </div>
      </Card>

      <Modal open={confirm !== null} title={`Disconnect ${confirm?.email_address}?`} onClose={() => setConfirm(null)}
             footer={<>
               <button type="button" className={buttonClass()} onClick={() => setConfirm(null)}>Cancel</button>
               <button type="button" className={buttonClass("danger")} disabled={busy !== null}
                       onClick={() => confirm && disconnect(confirm)}>Disconnect</button>
             </>}>
        <p className="text-sm">The stored token is deleted and nothing more can be sent from this mailbox. Campaigns using it
          can&apos;t send until you choose another mailbox or reconnect.</p>
      </Modal>
    </div>
  );
}
