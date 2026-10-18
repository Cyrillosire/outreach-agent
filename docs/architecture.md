# Architecture

## Shape: a modular monolith with workers

```
 Browser ──► Next.js (dashboard, SSR) ──/api/* rewrite──► FastAPI (/api/v1)
                                                             │
                     ┌───────────────────────────────────────┼─────────────────────────────┐
                     ▼                                       ▼                             ▼
               PostgreSQL (source of truth)            Redis (rate limits,          Workers (M3+)
               state, outbox, audit, evidence           cache, coordination)        research, drafting,
                                                                                    sending, sync
                                                                                         │
                                       ┌───────────────┬────────────────┬────────────────┤
                                       ▼               ▼                ▼                ▼
                                   Search API     Page fetcher      LLM provider    Gmail API /
                                   (adapter)      (SSRF-guarded)    (adapter)       Microsoft Graph
```

One deployable backend codebase with strict internal module boundaries. The API and workers share models
and services but run as separate processes. Microservices are deliberately avoided at this scale
(batches of up to 150 prospects).

### Module responsibilities

| Module | Responsibility | Milestone |
|---|---|---|
| `app/core` | config, logging and redaction, crypto, security primitives, errors, metrics, rate limiting | 1 ✅ |
| `app/domain/states.py` | lifecycle state machines: the only place transition rules live | 1 ✅ |
| `app/db/transitions.py` | atomic compare-and-set state updates | 1 ✅ |
| `app/services/auth`, `audit` | sessions, users, append-only audit log | 1 ✅ |
| `app/services/imports` | CSV parsing, mapping, validation, normalization, dedupe, provenance | 2 ✅ |
| `app/services/prospects`, `companies`, `suppression`, `idempotency` | search, edits, company upsert, suppression side effects, Idempotency-Key | 2 ✅ |
| `app/services/research` | orchestration, company + person research, evidence, sufficiency | 3 ✅ |
| `app/jobs` | Postgres-backed durable queue with leases, retries, dead-lettering; async worker | 3 ✅ |
| `app/integrations` | Groq/Ollama LLMs, Tavily/Brave search, SSRF-guarded fetcher, extraction | 3 ✅ |
| personalization | evidence selection, generation, claim validation, scoring, versioning | 4 |
| providers/gmail, providers/graph | OAuth, send, sync, notifications behind one interface | 5 |
| sequencing, suppression, sync | scheduling, outbox dispatch, reply detection, suppression | 6 |

Rule: HTTP handlers never contain business logic, and services never import provider SDKs directly.
Providers sit behind interfaces so tests use fakes and production uses only the real adapters.

## Key decisions and tradeoffs

1. **Durable queue on PostgreSQL first, not Temporal.** At 150 prospects per batch, a jobs table claimed
   with `SELECT … FOR UPDATE SKIP LOCKED` plus leases gives durability and crash recovery with one less
   system to operate. Workflow state lives in the same database as business state, so transitions and
   job enqueueing commit atomically. Revisit Temporal if workflows grow long-running or branchy enough to
   need its history and versioning model.
2. **Synchronous SQLAlchemy in the API.** Transactional state code is easier to reason about this way,
   and it avoids async-driver event-loop issues (psycopg async does not support Windows' default loop).
   Research fan-out will use async HTTP inside workers, with short explicit DB transactions.
3. **Enums stored as VARCHAR + CHECK, not native PG enums.** Values can be added or removed in one migration
   statement. The database still rejects unknown values.
4. **State machines in code, enforced in SQL.** `StateMachine` defines the allowed transitions, and
   `transition_row` turns them into `UPDATE … WHERE state IN (allowed predecessors)`. A writer that
   loses a race gets `StaleTransition` and cannot silently overwrite. This is how reply ingestion beats a
   concurrently scheduled send.
5. **Workspace tenancy from day one.** Every tenant-owned row carries `workspace_id`, and uniqueness is
   scoped per workspace. Adding tenancy later would require rewriting every query and constraint.
6. **Server-side opaque sessions, not JWTs.** Logout and revocation take effect immediately, and only
   SHA-256 hashes of session and CSRF tokens are stored. The dashboard proxies `/api` so cookies are
   first-party (HttpOnly, SameSite=Lax).
7. **No public signup.** Users are created by an operator CLI. This is a tool for one sales team, not
   a SaaS product.
8. **Next.js `cacheComponents` disabled.** Every dashboard page is session-scoped and dynamic, so there is
   nothing to prerender.

## Data model (migration 0001)

```
workspaces ─┬─ users ── user_sessions
            ├─ companies ──┬── prospects ── campaign_prospects ── campaigns ── sequences ── sequence_steps
            │              │        │                                  │
            │              │        ├── email_drafts ── email_draft_evidence ── research_facts
            │              │        └── email_messages ── email_accounts
            ├─ research_runs (company XOR prospect)
            ├─ research_sources ── research_fact_evidence ── research_facts (company XOR prospect)
            ├─ scheduled_actions (durable outbox)
            ├─ suppressions (email XOR domain; workspace or campaign scope)
            └─ audit_logs (append-only)
provider_events (deduplicated webhook/sync events)
```

Database-enforced guarantees (each one covered by an integration test):

| Guarantee | Constraint |
|---|---|
| One person per normalized work email per workspace | `uq_prospects_workspace_email` (partial) |
| Same name ≠ same person; no-email prospects allowed | no name-based uniqueness |
| Company research reused per normalized domain | `uq_companies_workspace_domain` (partial) |
| Company vs individual facts never mixed | `ck_research_facts_exactly_one_subject` |
| Initial email always human-approved | `ck_sequence_steps_initial_requires_approval` |
| Approval bound to approver, time and content hash | `ck_email_drafts_approval_complete` |
| Exactly one current draft version per step | `uq_email_drafts_current` (partial) |
| One logical send = one action and one message row | `uq_scheduled_actions_idempotency_key`, `uq_email_messages_send_key` |
| Webhook/sync replays are no-ops | `uq_provider_events_provider_dedupe_key` |

Tables beyond those listed in the brief, and why:
- `workspaces`: tenant boundary.
- `user_sessions`: revocable server-side sessions.
- `sequences`: needed by the `/sequences` endpoints. Steps belong to a sequence.
- `research_fact_evidence`: corroboration means one fact can have many supporting or contradicting sources.
- `email_draft_evidence`: a foreign-key link from each draft claim to the fact supporting it.

## Prospect import (Milestone 2)

Upload → map & validate (repeatable) → commit, all persisted, so an import can be resumed or reviewed later.

- **Provenance first.** Every uploaded row is stored verbatim (`prospect_import_rows.raw`) before any
  interpretation, along with its normalized form, its issues, and why it was or was not imported. Each
  prospect also carries its source row and row number.
- **Never guess.** Placeholder cells ("N/A", "-", "not found") are treated as empty. Full names are never
  split. A cell holding several emails is an error, not a guess. Freemail domains never become company
  domains. Ambiguous headers ("Other Emails (Company / General)") are never auto-mapped, and unknown
  columns default to *ignore*.
- **Identity.** One prospect per lowercased email per workspace (`+tags` and dots are kept). A shared name
  at the same company is flagged as a possible duplicate but both rows are imported.
- **Company domain precedence:** explicit domain column, then website, then a work-email domain. The
  source is recorded as `attributes.company_domain_source`. Sub-domains are not merged (no public-suffix
  guessing).
- **Commit is idempotent and race-safe:** a compare-and-set `validated → importing` serializes concurrent
  commits (tested with 3 simultaneous transactions). Prospects use `INSERT … ON CONFLICT DO NOTHING`, so
  people created after validation are reported as "already exists", never duplicated or overwritten.
  Companies are upserted on the partial unique `(workspace_id, normalized_domain)` index.
- **Limits:** 5 MB, 5,000 rows, 100 columns (configurable via `IMPORT_MAX_*`). `.xlsx` is rejected with a
  "save as CSV" message.
- **Research jobs are not created yet.** The durable queue arrives in Milestone 3, which will enqueue
  research for imported prospects. Until then, prospects stay `pending`.

`Idempotency-Key` is supported on `POST /imports` and `POST /prospects`. The key row is inserted in the
same transaction as the work, so a concurrent duplicate blocks on the unique index and then replays the
stored response. Reusing a key with a different body returns 422.

Suppression immediately moves affected campaign enrollments to `suppressed` and cancels every cancellable
scheduled action in the same transaction. In-flight (`executing`) sends are left to reconciliation.
Domain suppression of freemail providers is refused.

## Research engine (Milestone 3)

```
import/create ──► enqueue research.company (dedupe per company) + research.prospect (per person)
                          │                                   │
                worker (asyncio, bounded concurrency, leases, retries with backoff, dead-letter)
                          │                                   │
   company site crawl + search ──► fetch (SSRF-guarded, robots, cache) ──► main-text extraction
                          │                                   │
             passage selection (only relevant chunks reach the LLM; cuts tokens about 5x)
                          │
   LLM extraction (strict JSON schema; Groq → other Groq models → local Ollama on quota exhaustion)
                          │
   deterministic verification: excerpt must appear VERBATIM in what the model saw; sensitive personal attributes
   dropped; identity check rejects namesakes; near-duplicates merged; cross-site agreement = corroborated;
   numeric disagreement = conflicting
                          │
   facts + evidence persisted ──► sufficiency rules ──► finalize: ready / needs_review / insufficient / failed
```

- **Durable queue.** `jobs` table claimed with `FOR UPDATE SKIP LOCKED` under a lease. Heartbeats extend the lease, and
  expired leases are reclaimed (crash recovery). Results from a worker whose lease was reclaimed are discarded. Retries
  use exponential backoff with jitter (or the provider's Retry-After), then dead-lettering. One active job per company
  and per prospect (partial unique index). All queue timing uses the database clock.
- **Company research is shared**: one job per company, reused while fresh (`RESEARCH_COMPANY_FRESHNESS_DAYS`).
  Individual research runs concurrently and checks the company's people pages itself (via a shared cache) so it never
  waits on the company job.
- **Evidence over model memory.** Search results are only leads; facts come only from pages the fetcher retrieved, with
  the exact excerpt stored. Web content is delimited with an unpredictable boundary and treated as data, so the model
  is never told to act on it. The LLM layer has no tools and no access to sending, approval, or credentials.
- **Sufficiency (deterministic).** Company: at least 3 verified facts covering at least 2 of {what it does, recent
  developments, priorities/strategy/technology}. Individual: current role confirmed by a source, plus at least one more
  verified fact. A job title from the CSV is never treated as evidence. Ready only when both are sufficient; one-sided
  or conflicting evidence goes to human review.
- **Failure is explicit.** If every model is unavailable, the job retries later; it never reports "no evidence" for an
  outage. When retries run out the prospect becomes `failed_retryable` (bulk "Retry failed" in the UI). A missing LLM
  configuration is a permanent, clearly-worded failure.
- **Freshness.** Facts expire by category (for example procurement 60 days, announcements 120 days, overview 1 year).
  Re-research expires a subject's previous facts rather than deleting them, because drafts in Milestone 4 will
  reference them.

## State machines

Defined in `backend/app/domain/states.py`, which has 100% test coverage. Highlights:

- **Enrollment** (per prospect per campaign): every non-terminal state can move to `replied`,
  `suppressed`, `unsubscribed`, `bounced`, or `cancelled` without a human, so automated stop signals always
  win. Leaving `replied`, `paused`, or `suppressed` back into the sequence requires a human actor.
- **Scheduled action** (outbox): `pending → claimed → executing → succeeded | failed_retryable |
  ambiguous | dead`. Once `executing`, the action **cannot be cancelled**, because the provider call may be
  in flight. `ambiguous` (outcome unknown) can never return to `pending`. It must be reconciled
  against provider message history first.
- **Outbound message**: there is no `delivered` state. `provider_accepted` means only that the API
  accepted the request.
- **Draft**: `pending_review → approved` requires a human. Any edit or regeneration creates a new version
  and marks the old one `superseded`, so approval never carries over.
- **Research**: `ready_for_personalization` is reachable only through `verifying`, or by a human after
  `needs_review`.

## Planned send path (Milestone 6)

1. In one transaction: create a `scheduled_actions` row with key `send:{campaign_prospect_id}:step:{n}`.
2. Worker claims it (`pending → claimed`, with a lease) using SKIP LOCKED.
3. Worker re-checks in one transaction while holding a row lock on the `campaign_prospects` row: draft approved
   and content hash unchanged, enrollment sendable, no active suppression, campaign active, account
   connected, limits not exceeded. Then it moves to `executing` and inserts the `email_messages` row
   (`sending`, `send_idempotency_key`), and commits.
4. Provider call. Success → `provider_accepted` plus provider IDs. A timeout or crash leaves the action
   `executing`. Lease expiry moves it to `ambiguous`, never back to `pending`.
5. Reconciler searches the mailbox for the RFC `Message-ID` it generated before deciding between
   `succeeded` and a retry.

Reply ingestion runs in its own transaction: it persists the event, transitions enrollment to `replied`,
and cancels every cancellable action via compare-and-set. Step 3's row lock and state recheck guarantee
that a send which has not reached `executing` will see the reply.

## External services and credentials

| Service | Needed for | When | Credential |
|---|---|---|---|
| PostgreSQL 17 | everything | M1 ✅ | `DATABASE_URL` |
| Redis 7 | rate limiting, caching, coordination | M1 ✅ | `REDIS_URL` |
| Google Cloud project + OAuth client | Gmail send/read (`gmail.send`, `gmail.readonly` or `gmail.modify`), Pub/Sub push | M5 | client ID/secret, Pub/Sub topic |
| Microsoft Entra app registration | Graph `Mail.Send`, `Mail.Read`, `offline_access`; change notifications | M5 | client ID/secret, tenant config |
| LLM provider | extraction, reasoning, drafting | M3–4 | API key |
| Web search API | source discovery | M3 | API key |

Gmail restricted scopes require Google's OAuth verification (and possibly a security assessment) before
use by accounts outside your own organization. Plan for that lead time.
