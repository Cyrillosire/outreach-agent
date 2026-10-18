# Security and compliance checklist

Status legend: ✅ implemented and tested · 🟡 partially implemented · ⬜ planned (milestone)

## Authentication and sessions
- ✅ Argon2id password hashing; minimum 12 characters; transparent rehash on parameter changes
- ✅ Opaque 256-bit session tokens; only SHA-256 digests stored; server-side revocation on logout
- ✅ Session cookie `HttpOnly`, `SameSite=Lax`, `Secure` (enforced in production)
- ✅ Session expiry enforced server-side; inactive users rejected
- ✅ Constant-time behavior for unknown users (dummy hash verification); identical error for bad email or
  bad password
- ✅ Login rate limiting per IP+email and per IP (Redis). **Fails closed** (503) if Redis is down
- ✅ No public signup. Users are created by the operator CLI. Passwords are never accepted via argv
- ⬜ MFA / SSO (not in scope for M1; recommended before multi-team use)

## Request integrity
- ✅ CSRF: per-session synchronizer token (`X-CSRF-Token`, compared against a stored hash) on every unsafe
  authenticated request
- ✅ Origin check middleware rejects cross-origin unsafe requests, including login (blocks login CSRF)
- ✅ Security headers on API and dashboard (`nosniff`, `DENY` framing, referrer policy, restrictive CSP on
  API)
- ✅ Validation errors never echo submitted values (passwords cannot round-trip)
- ✅ Unhandled errors return a generic 500 with a request id; details only in logs
- ✅ `Idempotency-Key` on retryable creates (`POST /imports`, `POST /prospects`); replay, reuse detection, and concurrency tested
- ✅ CSV upload limits (size, rows, columns, cell length); binary and spreadsheet files rejected; cell values never evaluated (formula-like values stored as plain text)
- ✅ LIKE wildcards escaped in search; all prospect, company, and import reads are workspace-scoped (cross-workspace access returns 404, tested)

## Data protection
- ✅ AES-256-GCM credential encryption with versioned keys (rotation supported), ciphertext bound to its
  row via associated data. Access tokens are never persisted
- ✅ Secrets are only in env/secrets manager; `SecretStr` prevents accidental repr/log exposure
- ✅ Log redaction: sensitive keys (tokens, passwords, cookies, message bodies) and token-shaped values
  (Bearer, Google tokens, JWTs, DSN passwords) are masked in every log event and in audit metadata
- ✅ SQL echo disabled; no query strings/headers/bodies in request logs
- ✅ Tenant isolation: every tenant-owned row carries `workspace_id`; dashboard queries are scoped (tested)
- ⬜ Data export and deletion endpoints; configurable retention for message bodies (M7)
- ⬜ Database backups and encryption at rest (deployment responsibility; documented in M7)

## Auditability
- ✅ Append-only `audit_logs` (actor, action, entity, outcome, request id, IP); login success and failure,
  logout, and user creation are recorded today
- ✅ Import upload/map/commit/cancel, prospect create/update (field names only, never values), suppression
- ⬜ Approvals, sends, replies, account connect/disconnect (recorded as each feature lands)

## AI and research safety (M3)
- ✅ SSRF defenses: http(s) on ports 80/443 only, no URL credentials, and every DNS answer must be public (rejects
  private, loopback, link-local and metadata IPs, IPv4-mapped IPv6 included). Redirects are followed manually and
  re-validated per hop, and the connected peer address is re-checked (DNS-rebinding guard). Size and time caps apply,
  and no credentials or cookies are ever sent. Verified on the live network from the worker container.
- ✅ robots.txt respected (RFC 9309 semantics), per-host politeness delay, identifying User-Agent; login-walled and
  terms-restricted sites (LinkedIn, Facebook, data brokers, …) are never fetched
- ✅ Prompt-injection containment: web text sits inside an unpredictable boundary with explicit "never follow
  instructions in the source" rules. The LLM has no tools; output is schema-validated; nothing an LLM returns can
  trigger an action (tested with an injected instruction)
- ✅ The LLM's own knowledge is never evidence: every stored fact carries a verbatim excerpt that is mechanically
  checked against the fetched page (paraphrased or invented excerpts are discarded, tested)
- ✅ Sensitive personal attributes (health, religion, family, politics, …) are filtered from individual research; a
  namesake guard requires pages to name both the person and their organization
- ✅ LLM provider data handling: Groq terms bar training on API data, with zero data retention available. Gemini's free
  tier was rejected because its terms allow training and human review and forbid personal data. Local Ollama keeps
  data on the machine.
- ✅ API keys only in `.env`/secrets; never logged (structlog redaction), never sent to the frontend

## Drafting safety (M4)
- ✅ Nothing is sent or approved by AI. Approval is a human action bound to the exact content hash; any edit or
  regeneration creates a new version without approval (tested)
- ✅ Claims are mechanically checked: every fact must cite research or offering refs, unsupported numbers and
  forbidden claims are blocking errors, and a second model reviews for unsupported statements (tested with fakes and
  with live models)
- ✅ Drafting uses hosted models only (no local fallback): accuracy over cost for anything a prospect reads
- ✅ Sensitive personal content, false familiarity, fake urgency, and links other than the campaign's are rejected
- ✅ Weak or disqualified fits are held for a human decision instead of being drafted silently
- ✅ Internal offering notes and proof-point sources are never sent to the writing model
- ✅ Compliance footer (sender identity, postal address, opt-out) is required before a campaign can draft

## Email provider integration (M5–M6)
- ⬜ OAuth 2.0 authorization-code flow with PKCE, state bound to the session, exact redirect URIs
- ⬜ Least-privilege scopes requested per enabled feature; granted scopes recorded per account
- ⬜ Refresh-token rotation handling; explicit disconnect revokes and deletes local credentials
- ⬜ Webhook authenticity: Pub/Sub push auth (OIDC token) for Gmail; `clientState` validation for Graph
- ⬜ Never claim delivery: `provider_accepted` only; bounces and replies detected via sync

## Lawful outreach: required before launch
The software enforces mechanics, but legal compliance depends on how it is used. Before sending, the
operator must confirm:
- ⬜ A lawful basis for contacting each recipient in their jurisdiction (e.g. GDPR legitimate-interest
  assessment for EU/UK B2B contacts; CASL consent rules for Canada; India's DPDP Act for Indian data
  principals; CAN-SPAM for the US)
- ⬜ Accurate sender identity and a valid postal address in every email (workspace fields exist:
  `legal_sender_name`, `postal_address`)
- ⬜ A working opt-out in every email, honored promptly and permanently (suppression model exists;
  unsubscribe handling lands in M6)
- ⬜ No misleading subject lines or headers
- ⬜ SPF, DKIM, and DMARC configured for each sending domain
- ⬜ Compliance with Google Workspace / Gmail and Microsoft 365 acceptable-use policies and per-account
  sending limits. A connected mailbox does not make a cold campaign compliant
- ⬜ A privacy notice covering prospect data sourcing, retention, and data-subject rights requests

## Known limitations (Milestone 4)
- The AI reviewer's judgement varies between runs; drafts it flags stay pending with the reason shown rather than
  being discarded. Deterministic rules are stable.
- Hedging is detected by phrasing heuristics (questions, "if…", "may…"); an unusual phrasing can be misjudged.
- Sending is not connected yet (M5), so activating a campaign reports `no_sending_mailbox`.

## Known limitations (Milestone 3)
- The fetcher does not execute JavaScript, so script-rendered sites yield thin text (they show their no-script
  fallback). PDF annual reports are not parsed yet.
- Pages fetched while checking a possible namesake are stored as sources even when rejected (they are never linked to
  a fact). Retention cleanup arrives with Milestone 7.
- Contradiction detection is heuristic (same topic, different numbers, different sites); subtle textual conflicts
  are not detected.
- The local-model fallback is slow on this hardware (minutes per page); research quality and speed assume Groq for
  most extraction.

## Known limitations (Milestone 2)
- Domainless companies are matched by exact case-insensitive name. That match is not constraint-protected,
  so two simultaneous imports could create two records for the same domainless company.
- Suppressions cannot be lifted from the UI or API yet (they are permanent; a review-policy flow is planned).
- CSV export, when added, must escape formula-leading cells (`= + - @`) to prevent spreadsheet injection.
- Prospect edits are last-write-wins (no optimistic concurrency token yet).

## Known limitations (Milestone 1)
- The rate limiter fails closed when Redis is down, so an outage blocks logins (intentional).
- Client IP depends on correct proxy configuration (`--proxy-headers` with trusted IPs only).
- `/metrics` is unauthenticated and must be kept off the public network.
- No account lockout notifications or password reset flow yet. Admins reset passwords via the CLI or database.
