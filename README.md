# BotChain AI — Backend

FastAPI backend that turns plain-language business processes into **ready-to-import,
validated n8n workflows** — no knowledge of nodes, APIs, or JSON required.

A business user describes a problem or process in natural language. A two-phase
conversational agent (Plan → Build) interviews them into a structured requirements
spec, then generates a validated n8n workflow JSON — grounding every node-level
decision in live n8n documentation via the n8n-mcp server (1,600+ nodes) rather than
model memory, and validating the result programmatically before delivery.

The Next.js frontend (`botchain-ai-next-app`, separate repo) consumes this API through
its own proxy routes.

**Live in production:** <https://botchain-ai-production.up.railway.app> — `/health` returns
`200`, and `GET /api/v1/chats` with a real Kinde token returns `200` against production
Neon. Deployed by a CI-gated `railway up`; see [Deployment](#deployment).

## Current status

**Phases 0–7, 9 and 10 are done. Phase 8 is deferred by choice** — see the table note below.

| Phase | Milestone | Status |
|---|---|---|
| 0 | Repo scaffold (`src/app/`, config, deps) | Done |
| 1 | Database — 7 SQLAlchemy models matching the live Prisma schema, zero-diff Alembic baseline `3169311c48d2` | Done |
| 2 | Auth — Kinde JWT via JWKS, read-only `current_user` | Done |
| 3 | Checkpointing — `AsyncPostgresSaver` on the same DB | Done |
| 4 | Core routes — chats CRUD, messages, approve | Done |
| 5 | LangGraph Plan→Confirm→Build→Validate flow, approval interrupt, validated end-to-end | Done |
| 6 | Streaming — AI SDK v7 **Data Stream Protocol** (UI Message Stream) | Done |
| 6.6 | Chat lifecycle + account purge (`DELETE /me/data`) | Done |
| 7 | Sandbox/files — per-turn ephemeral scratch, workflow persisted to `Message.meta` | Done |
| 8 | Tests — dedicated `tests` Neon branch | **Deferred by choice.** The suite is green (135 tests), but the branch does not exist yet, so the 3 real-DB checkpoint tests resolve `DATABASE_URL` to **production** and are marked `db` / excluded from CI. Resumes when the branch lands |
| 9 | Containerize + CI (Dockerfile, GitHub Actions) | Done |
| 10 | First deploy (Railway) | Done |

The build checklist, settled architecture decisions, and phase-level implementation
records are the source of truth — see [Source-of-truth docs](#source-of-truth-docs).

## Quickstart

### Prerequisites

- **Python 3.14.6** (pinned in `.python-version`) and [`uv`](https://docs.astral.sh/uv/).
- A **`.env`** file. Copy `.env.example`, then fill in real values — at minimum
  `DATABASE_URL`, `KINDE_ISSUER_URL`, `MISTRAL_API_KEY`. `.env` is gitignored;
  **never commit it.**

```dotenv
# Required
DATABASE_URL=postgresql://user:password@host.neon.tech/neondb?sslmode=require   # direct/unpooled
KINDE_ISSUER_URL=https://your-app.kinde.com
MISTRAL_API_KEY=

# Optional / when needed
KINDE_AUDIENCE=       # leave empty; audience checks are skipped when unset
OLLAMA_API_KEY=       # only for the commented-out ChatOllama path in services/llm.py
N8N_API_URL=          # only for n8n-mcp's 16 management tools
N8N_API_KEY=
CORS_ORIGINS=http://localhost:3000
```

> The backend must use the **direct (unpooled)** Neon URL — never the pooled `DATABASE_URL`
> the frontend uses.

> The model is Mistral (`ministral-14b-latest`) behind `src/app/services/llm.py`; the
> Ollama path is kept commented out in that one file, so swapping providers touches one
> file. `Chat.model` stores the real model name on each chat.

### Install

```sh
uv sync
```

### Run the API

```sh
uv run uvicorn app.main:app --reload
```

- App: <http://127.0.0.1:8000>
- Health check: `GET /health` → `200 {"status": "ok"}`
- Docs: <http://127.0.0.1:8000/docs>

### Run tests

```sh
uv run pytest                    # whole suite (135 tests)
uv run pytest -m "not db"        # 132 — skips the 3 real-DB checkpoint tests
uv run pytest -k streaming       # a single area
uv run pytest tests/test_security.py
```

> **The `tests` Neon branch does not exist yet.** `tests/test_checkpoint.py` is the only
> suite that touches a real database, and it reads `DATABASE_URL` — so locally it resolves
> to the **production** branch unless you point it elsewhere. Those 3 tests carry
> `pytestmark = pytest.mark.db` and CI runs `pytest -m "not db"`, which keeps CI entirely
> secret-free (wiring the production URL into CI secrets would make every PR write to
> production). When the `tests` branch lands, add a second CI job running `-m db` against
> it rather than widening the existing one.

### Lint

```sh
uv run ruff check src tests
```

### Database migrations

The tables are **Prisma-owned** (created by the frontend) — baseline, don't recreate.

```sh
uv run alembic revision --autogenerate -m "..."   # confirm an empty/near-empty diff
uv run alembic upgrade head                       # or `stamp head` for the Prisma baseline
```

LangGraph checkpoint tables are LangGraph-owned and created at startup — they are
**not** part of the Alembic-managed schema.

## How it works

### The graph

```
START → plan_node ⇄ (loops with the user until the spec is complete & no open questions)
              ↓
        confirm_node (interrupt: plain-English spec summary — build it?)
              ↓  (user confirms via POST /api/v1/chats/{id}/approve)
        build_node → validate_node → (pass) → END
                          ↓ (fail, retry_count < 3)
                     build_node (self-correct on validation errors, ≤3 retries)
```

- `plan_node` fills a structured `RequirementsSpec` (goal, trigger, services,
  conditions, data flow, constraints, open questions) one question at a time.
- `confirm_node` is a LangGraph `interrupt()` surfaced to the user for explicit approval.
- `build_node` binds the ~7 core n8n-mcp tools + a sandboxed `write_json_file` and
  assembles the workflow, grounding every node in live tool lookups (cached
  in-memory).
- `validate_node` validates via n8n's `validate_workflow` and self-repairs
  (≤3 retries); schema validity ≠ logical correctness, so it re-checks the spec vs.
  the workflow before delivery. On non-convergence: best-effort JSON + remaining
  issues, never a silent fail.

### API surface (`/api/v1`, user-scoped via Kinde JWT)

| Method | Route | Purpose |
|---|---|---|
| POST | `/chats` | create chat |
| GET | `/chats` | list user's chats |
| GET/PATCH/DELETE | `/chats/{chat_id}` | read / rename (strip, 120-char cap) / soft-delete (409 while a stream holds the chat) |
| GET | `/chats/{chat_id}/messages` | history (resume) |
| POST | `/chats/{chat_id}/messages` | send message → streaming reply |
| POST | `/chats/{chat_id}/approve` | resume interrupted graph (approved + feedback) |
| GET | `/chats/{chat_id}/messages/{message_id}/attachments/{attachment_id}/download` | workflow file download (served from `Message.meta`) |
| DELETE | `/me/data` | purge the current user's data — chats (incl. soft-deleted) + checkpoint threads, messages, attachments, billing. Never touches `users`; 409 if any chat is mid-stream |
| GET | `/credits` | stub (balance) |
| POST | `/webhooks/*` | stub (payments — Razorpay later) |

### Streaming contract

Message/approve routes return SSE in the AI SDK v7 **Data Stream Protocol** (UI Message
Stream): `start` → transient `data-status` progress heartbeats → `text-start` /
`text-delta*` / `text-end` (fresh uuid part id per text segment) → `finish` →
`[DONE]`, with header `x-vercel-ai-ui-message-stream: v1`. `useChat()` in the frontend
consumes it with zero config. Full wire spec: `_markdown/backend-api-schema.md` §5.

## Deployment

Deployed to **Railway** at <https://botchain-ai-production.up.railway.app>, one replica in
Southeast Asia (matching Neon `ap-southeast-1`).

- **CI-driven.** `.github/workflows/ci.yml` runs `lint`, `test`, `package` and `docker`, and
  the `deploy` job (`railway up`) gates on all four. It is inert on pull requests and only
  runs on `main` when the `RAILWAY_DEPLOY_ENABLED` repository variable is `"true"`.
- **Railway GitHub autodeploy is disabled on purpose.** A GitHub-linked service auto-deploys
  on every push and would ship regardless of CI, bypassing the gate. The only path to
  production is the `needs`-gated job.
- `RAILWAY_TOKEN` is scoped to the GitHub **`production` environment**, not the repo, so it
  is unreadable from pull requests.
- Runtime variables are set by hand in Railway as **sealed** values — never by importing
  `.env`. Required: `DATABASE_URL` (Neon direct/unpooled), `MISTRAL_API_KEY`,
  `KINDE_ISSUER_URL`. `N8N_API_*` may stay empty (the 7 core MCP tools work without an n8n
  instance).

Two things to know before touching the deploy config:

- **The domain's target port must be `8080`.** Railway injects `PORT=8080` and the `CMD`
  honors it (`--port ${PORT:-8000}`), so uvicorn binds 8080. Pointing the domain at 8000
  produces an edge `502` from a perfectly healthy container.
- **`CORS_ORIGINS` is unset**, so no CORS middleware is installed. That is *correct* for the
  current frontend: it proxies every backend call server-side (all four files touching
  `BACKEND_URL` are route handlers or server code, with no `NEXT_PUBLIC_*` anywhere), so the
  browser never calls FastAPI and CORS never applies. Set it only if something ever calls
  the API directly from a browser.

Full first-deploy procedure, both production incidents (and why each passed every gate), and
the rollback/stop-the-line steps: **`_markdown/phase10-deploy-runbook.md`**.

## Repo map

```
├── src/app/                  production backend (FastAPI)
│   ├── main.py               app factory, lifespan, routers, n8n-mcp launch
│   ├── config.py             pydantic-settings (env-driven)
│   ├── db.py                 async engine + session factory
│   ├── deps.py               current_user, pagination, ownership
│   ├── api/v1/               chats.py, messages.py, credits.py, webhooks.py
│   ├── models/               SQLAlchemy models (match contract.prisma)
│   ├── schemas/              request/response DTOs
│   ├── services/             agent.py (LangGraph), llm.py, checkpoint.py, context.py,
│   │                         pricing.py, billing.py, chat_locks.py, account.py, payments/
│   ├── core/                 security.py (Kinde JWKS), exceptions.py
│   ├── streaming.py          Data Stream Protocol encoder
│   └── prompts/              plan/build/repair + guardrails (shipped in the wheel — see below)
├── tests/                    pytest suite (135 tests)
├── alembic/                  migrations (zero-diff baseline only)
├── Dockerfile                multi-stage: uv build -> slim runtime, node for n8n-mcp
├── railway.json              Railway builder config (deprecated 2026-12-01)
├── _markdown/                SOURCE OF TRUTH — checklist, settled decisions, plans, records
├── prototype/                working single-agent prototype (terminal UI, SQLite)
├── prompts/                  prototype-only prompt placeholders
└── notebooks/                prototyping + testcase prompts
```

> **`.gitignore` patterns for root-only directories must be anchored** (`/prompts`, not
> `prompts/`). An unanchored pattern matches at any depth, so `prompts/` also matched
> `src/app/prompts/` — and because hatchling excludes VCS-ignored paths, production code was
> silently dropped from the wheel while still looking present in `git`. The `package` CI job
> now guards this.

## Source-of-truth docs

Read in this order — they define what's built and why; don't decide divergences silently:

1. `_markdown/python-fastapi-backendchecklist.md` — the 10-phase checklist + folder structure.
2. `_markdown/backend-setup-qa.md` — settled architecture decisions (Q1–Q10).
3. `_nextjs_repo_context/prisma/contract.prisma` — the **live Neon schema** this backend mirrors.
4. `_markdown/backend-api-schema.md` — the API + streaming contract the frontend codes against.

Implementation records per phase: `_markdown/phase5-implementation.md`,
`_markdown/phase6-implementation.md`, `_markdown/phase6/` (plans + wire spec),
`_markdown/phase9/backend-phase9-plan.md` (containerize + CI), and
`_markdown/phase10-deploy-runbook.md` (first deploy, production incidents, rollback).

## Guardrails

- Never fabricate node types, parameters, credentials, or endpoints — ground via MCP lookups.
- Never write real secrets into workflow JSON — empty credential placeholders only.
- Never call a workflow "done" without passing validation **and** explicit human approval.
- Schema validity ≠ logical correctness — re-read spec vs. workflow before delivery.
- `users` is Prisma/Next.js-owned; the backend is a **read-only consumer** (401 if the
  User row is missing — a real flow error, never papered over).
- Never commit `.env`, credentials, or pasted tokens. Debug with `read -rs VAR` on the
  machine that owns the secret — a token pasted into a transcript can arrive corrupted in a
  way that manufactures convincing false failures.