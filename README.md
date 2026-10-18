# Outreach Agent

An AI-native, evidence-based prospect research and outreach platform. It imports a prospect list,
researches each person and company with cited sources, drafts personalized emails grounded in that
evidence, sends **human-approved** emails through connected Gmail or Microsoft 365 mailboxes, and stops
follow-ups the moment a prospect replies.

> **Status: Milestone 4 (personalization and draft review) complete.** Define what you sell as **offerings**, build
> **campaigns** from a playbook (product sale, partnership, pilot, service, event, research) with a structured brief,
> and the drafting engine writes evidence-grounded emails: every claim cites research or offering facts, rules and an
> AI reviewer check them, and you review, edit, and approve each exact version. Sending, sequences, and reply sync are
> **not built yet** (M5 and M6). See [Roadmap](#roadmap). Provider keys: [docs/provider-setup.md](docs/provider-setup.md).

## Repository layout

```
backend/            FastAPI app, SQLAlchemy models, Alembic migrations, tests
  app/core/         config, logging (redaction), crypto, security, errors, metrics, rate limiting
  app/domain/       state machines: the single source of truth for lifecycle rules
  app/db/           engine/session, declarative base, race-safe compare-and-set transitions
  app/models/       ORM models (identity, prospects, research, campaigns, email, operations)
  app/services/     business logic (no HTTP, no provider SDKs)
  app/api/          HTTP layer: dependencies, middleware, /healthz, /readyz, /api/v1/*
  alembic/          migrations (0001 = full core schema)
  tests/unit/       pure logic, no external services
  tests/integration real PostgreSQL + Redis
frontend/           Next.js 16 dashboard (App Router, TypeScript, Tailwind 4)
infra/              local infrastructure helpers (Postgres init)
docs/               architecture, decisions, security & compliance
docker-compose.yml  local stack: postgres, redis, backend, frontend
```

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker Desktop | 29.x | WSL2 backend on Windows |
| Python | 3.12 | backend development and tests on the host |
| Node.js | 24 LTS | frontend development on the host |

## Quick start (everything in Docker)

```bash
cp .env.example .env                      # placeholders only; nothing secret is required for local dev
docker compose up -d --build              # postgres, redis, backend (runs migrations), worker, frontend
docker compose exec backend python -m app.cli create-user \
    --email you@example.com --name "Your Name" --role admin --workspace "My Workspace"
# prompts for a password (min. 12 characters)
```

Open http://localhost:3000 and sign in. API docs: http://localhost:8000/api/docs.
Add research provider keys (Groq, Tavily, Brave) by following [docs/provider-setup.md](docs/provider-setup.md).

## Local development (hot reload on the host)

Run only the infrastructure in Docker:

```bash
docker compose up -d postgres redis
```

**Backend** (PowerShell shown; on macOS/Linux use `source .venv/bin/activate`):

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy ..\.env.example .env                  # then set CREDENTIAL_ENCRYPTION_KEYS:
python -m app.cli generate-encryption-key  # paste output into CREDENTIAL_ENCRYPTION_KEYS
alembic upgrade head
python -m app.cli create-user --email you@example.com --name "You" --role admin
uvicorn app.main:app --reload --port 8000
python -m app.worker                       # in a second terminal: background research jobs
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000, proxies /api/* to http://127.0.0.1:8000
```

> Use `127.0.0.1`, not `localhost`, in database/Redis URLs on Windows. `localhost` can resolve to IPv6
> `::1` while Compose publishes IPv4 only, and the connection then hangs instead of failing.

## Testing

```bash
docker compose up -d postgres redis      # integration tests need these (test DB: outreach_test, Redis db 15)

cd backend
ruff check .                             # lint
mypy app                                 # strict type checking
pytest                                   # unit + integration
pytest tests/unit                        # unit only; no services needed
pytest --cov=app --cov-report=term-missing
LIVE_LLM=1 pytest tests/integration/test_live_drafting.py -s   # opt-in: drafts with the real Groq models

cd ../frontend
npm run lint
npm run typecheck
npm test
npm run build
```

Integration tests rebuild the `outreach_test` schema from migrations (upgrade → downgrade → upgrade)
once per run, and roll back every test's transaction. If Postgres is unreachable they are **skipped
with a reason**, never reported as passed. CI (`.github/workflows/ci.yml`) runs all of the above.

## Secrets and the public repository

This repository is public. Real keys live only in `.env` (git-ignored); `.env.example` holds placeholders. After
cloning, run `sh scripts/install-hooks.sh` once: it installs a pre-push hook that runs `scripts/check_secrets.py` and
refuses to push API keys, private keys, OAuth client secrets, `.env` files, database dumps, or prospect CSVs. CI runs
the same check.

## Configuration

All configuration comes from environment variables (see `.env.example`). In production
(`APP_ENV=production`), the app **refuses to start** if `COOKIE_SECURE` is false,
`CREDENTIAL_ENCRYPTION_KEYS` is empty, the default database credentials are used, or a non-HTTPS origin is
allowed.

## Deployment notes (current state)

Milestone 1 is not yet a production deployment. Before any production use:

- Terminate TLS at a reverse proxy, set `APP_ENV=production`, `COOKIE_SECURE=true`, and
  `ALLOWED_ORIGINS=["https://your-domain"]`.
- Run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy IP>` so client IPs (rate limiting, audit)
  are correct.
- Do not expose `/metrics` publicly.
- Store `DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEYS`, and (later) OAuth secrets in a secrets manager.
- Build the frontend image with `--build-arg BACKEND_URL=<internal API URL>`. Rewrites are compiled at
  build time.
- Run `alembic upgrade head` as a release step, not on every container start, once multiple replicas exist.

The full deployment guide and launch checklist are Milestone 7 deliverables.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| 1 | Foundation: schema, auth, state machines, logging, metrics, Docker, dashboard shell | **Done** |
| 2 | CSV import, column mapping, validation, dedupe, prospect list/detail, suppression | **Done** |
| 3 | Research engine: search/extraction adapters, evidence store, durable job queue | **Done** |
| 4 | Offerings, campaign briefs and playbooks, personalization, claim validation, quality scoring, draft review, versioned approval | **Done** |
| 5 | Gmail + Microsoft Graph OAuth and sending (verified with real test accounts) | Next |
| 6 | Durable sequences, reply sync, suppression, ambiguous-send reconciliation | Planned |
| 7 | Analytics, hardening, deployment docs, staged launch | Planned |

See [docs/architecture.md](docs/architecture.md) and
[docs/security-and-compliance.md](docs/security-and-compliance.md).
