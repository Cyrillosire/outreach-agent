# Mailbox setup (Gmail and Microsoft 365)

To send, the app needs an **OAuth client**: a registration with Google and/or Microsoft that lets the app ask you
for permission to send email as you. You create it once; then anyone in the workspace connects their mailbox with a
normal "Sign in with Google/Microsoft" screen. You only need the provider(s) you actually send from.

What the app asks for (least privilege):

| Provider | Permissions | What it allows |
|---|---|---|
| Google | `openid`, `email`, `gmail.send` | Know which address connected; **send** email. It can't read your inbox. |
| Microsoft | `openid`, `email`, `offline_access`, `User.Read`, `Mail.Send` | Same: identify the account and **send**. |

Reading replies needs an extra, read-only permission in Milestone 6; you'll be asked to reconnect then.

> **Secrets** (client secrets) go only in `.env` on your machine (git-ignored). Never paste them in chat or commit them.
> The pre-push hook blocks pushes that contain one.

Redirect URIs (must match **exactly**, including `http`, port, and no trailing slash):
```
http://localhost:3000/api/v1/mailboxes/oauth/gmail/callback
http://localhost:3000/api/v1/mailboxes/oauth/microsoft/callback
```
Connect mailboxes from **http://localhost:3000** (not 127.0.0.1): your login cookie must be on the same address the
provider sends you back to. The Mailboxes page warns you if you're on the wrong one.

---

## A. Google (Gmail or Google Workspace)

1. Go to **https://console.cloud.google.com**, sign in with the account that will own the app, and create a project
   (top bar → project picker → **New project**, e.g. `outreach-agent`).
2. **Enable the Gmail API**: **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **Configure the consent screen** (**Google Auth Platform**, or **APIs & Services → OAuth consent screen** in the
   older UI):
   - **Branding**: app name `Outreach Agent`, your support email, developer contact email.
   - **Audience**:
     - If you send from a **Google Workspace** address (your company domain on Google), choose **Internal**. Only
       your organization can connect, no Google review is needed, and tokens don't expire. **Recommended.**
     - Otherwise choose **External** and leave it in **Testing**, then add every address that will connect under
       **Test users** (up to 100). Limitation: in Testing mode Google expires the authorization **after 7 days**; the
       mailbox then shows "Reconnect needed" and you click Reconnect. Removing this requires Google's app verification.
   - **Data access**: **Add or remove scopes** → add `.../auth/gmail.send` (plus `openid` and `email`). Google
     labels `gmail.send` as *sensitive*; for Testing/Internal apps that's fine.
4. **Create the client**: **Clients** (or **Credentials → Create credentials → OAuth client ID**):
   - Application type: **Web application**, name `outreach-agent-local`.
   - **Authorized redirect URIs** → add `http://localhost:3000/api/v1/mailboxes/oauth/gmail/callback`.
   - **Create**, then copy the **Client ID** and **Client secret** (download the JSON if offered, keep it outside the
     project folder, and delete it once the values are in `.env`).
5. Put them in `.env`:
   ```dotenv
   GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-your_secret
   ```

When you connect, Google may show **"Google hasn't verified this app"** (External + Testing). That's expected for
your own app: click **Continue**. Tick the permission to **send email on your behalf**; if you untick it, the app
refuses the connection.

## B. Microsoft 365 / Outlook

1. Go to **https://entra.microsoft.com** (Microsoft Entra admin center) and sign in with your work account.
   Registering apps needs a Microsoft Entra tenant: any Microsoft 365 business account has one. A personal
   outlook.com account alone can't register apps; use Gmail, or a Microsoft 365 business/developer tenant.
2. **Identity → Applications → App registrations → New registration**:
   - Name: `Outreach Agent`.
   - Supported account types: **Accounts in this organizational directory only** (simplest; then set
     `MICROSOFT_TENANT` to your **Directory (tenant) ID** from the Overview page), or "...any organizational directory
     and personal Microsoft accounts" (keep `MICROSOFT_TENANT=common`).
   - Redirect URI: platform **Web**, `http://localhost:3000/api/v1/mailboxes/oauth/microsoft/callback`.
   - **Register**. Copy the **Application (client) ID** from the Overview page.
3. **Certificates & secrets → Client secrets → New client secret**: description `outreach-agent-local`, expiry
   e.g. 12 months. **Copy the secret's *Value*** (not the "Secret ID") right away; it's shown only once. Note the
   expiry date: the app stops connecting new mailboxes when it expires, until you create a new secret.
4. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**: add `Mail.Send`, `User.Read`,
   `offline_access`, `openid`, `email`. If your organization requires admin approval, click **Grant admin consent**
   (or ask your Microsoft 365 admin).
5. Put them in `.env`:
   ```dotenv
   MICROSOFT_CLIENT_ID=00000000-0000-0000-0000-000000000000
   MICROSOFT_CLIENT_SECRET=your_secret_value
   MICROSOFT_TENANT=common
   ```

Disconnecting a Microsoft mailbox deletes the app's token, but Microsoft has no API for an app to revoke its own
access. To remove it completely, go to **https://myaccount.microsoft.com → Apps** (or an admin removes it in Entra).

---

## C. Restart and connect

```powershell
cd C:\Users\user\projects\outreach-agent
docker compose up -d --build backend worker
```

1. Open **http://localhost:3000**, log in, go to **Settings → Mailboxes**. Each provider you configured shows **Ready**.
2. Click **Connect Gmail** / **Connect Microsoft**, choose the account, and approve.
3. Back on the Mailboxes page, click **Send test email**: it goes to the mailbox itself, never to a prospect. It
   should show **Sent** and arrive within a minute.
4. In a campaign's brief, choose the mailbox under **Send from**. Approved first emails then show a **Send now**
   panel on the draft page.

## Sending safety (what the app guarantees)

- Only drafts you approved are sent, and only the exact approved text (bound to its content hash).
- Right before sending, the app re-checks: still the approved version, contact not unsubscribed or suppressed, the
  mailbox still connected, and the daily cap (`MAILBOX_DAILY_SEND_LIMIT`, default 40 per 24 hours).
- A double click or a worker retry can never send twice: one approved version = one send.
- If the provider's answer is lost (timeout), the message is marked **Outcome unknown** and **not** retried, to
  avoid a duplicate. Check the mailbox's Sent folder; automatic reconciliation arrives in Milestone 6.
- "Sent" means the provider accepted it. Delivery isn't guaranteed; bounces are detected in Milestone 6.
- Every email carries a `List-Unsubscribe` header and your compliance footer. Configure SPF, DKIM and DMARC for your
  sending domain (Google Workspace and Microsoft 365 admin guides) before real outreach.
