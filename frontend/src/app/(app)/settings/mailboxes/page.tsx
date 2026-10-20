import type { Metadata } from "next";
import Link from "next/link";

import { Notice, PageHeader } from "@/components/ui";
import { serverFetch } from "@/lib/api-server";
import { firstParam, type SearchParams } from "@/lib/query";
import type { Mailbox, MailProvidersInfo } from "@/lib/types";

import { MailboxManager } from "./mailbox-manager";

export const metadata: Metadata = { title: "Mailboxes" };

const CONNECT_ERRORS: Record<string, string> = {
  consent_declined: "You cancelled on the provider's consent screen. Nothing was connected.",
  missing_scope: "Sending permission wasn't granted. Connect again and allow sending email on your behalf.",
  state_reused: "That connection link was already used. Start the connection again.",
  state_expired: "The connection took longer than 10 minutes. Start it again.",
  invalid_state: "That connection link isn't valid. Start the connection again.",
  session_mismatch: "The connection was finished in a different login session. Start it again from here.",
  no_refresh_token: "The provider didn't grant offline access. Remove the app from your account's permissions and connect again.",
  provider_error: "The provider returned an error. Check the OAuth client settings (docs/provider-setup.md) and try again.",
  invalid_callback: "The provider's response was incomplete. Try again.",
};

export default async function MailboxesPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const params = await searchParams;
  const [info, mailboxes] = await Promise.all([
    serverFetch<MailProvidersInfo>("/mailboxes/providers"),
    serverFetch<Mailbox[]>("/mailboxes"),
  ]);
  const connected = firstParam(params.connected);
  const error = firstParam(params.error);

  return (
    <>
      <PageHeader
        title="Mailboxes"
        description="Connect the Gmail or Microsoft 365 accounts you send from. The app can only send email (it can't read your inbox yet), and only drafts you've approved."
      />
      <p className="-mt-4 mb-6 text-sm"><Link href="/settings" className="text-accent underline">← Settings</Link></p>
      {connected ? <div className="mb-4"><Notice tone="success" title={`Connected ${connected}.`}>Send a test email to confirm everything works.</Notice></div> : null}
      {error ? <div className="mb-4"><Notice tone="danger" title="The mailbox wasn't connected.">{CONNECT_ERRORS[error] ?? error}</Notice></div> : null}
      <MailboxManager info={info} mailboxes={mailboxes} />
    </>
  );
}
