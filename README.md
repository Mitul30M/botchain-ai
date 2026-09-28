# BotChain AI — Backend

FastAPI backend that turns plain-language business processes into **ready-to-import,
validated n8n workflows** — no knowledge of nodes, APIs, or JSON required.

A business user describes a problem or process in natural language. A two-phase
conversational agent (Plan → Build) interviews them into a structured requirements
spec, then generates a validated n8n workflow JSON — grounding every node-level
decision in live n8n documentation via the n8n-mcp server (1,600+ nodes) rather than
model memory, and validating the result programmatically before delivery.

The Next.js frontend (`botchain-ai-next-app`, separate repo) consumes this API through
its own proxy routes; backend↔frontend wiring is in progress on the frontend side.

## Current status

Production build in progress, working the 10-phase checklist in `_markdown/` phase by
phase. **Phases 1–6 verified done; Phase 7 (sandbox/files) is next.**

| Phase | Milestone | Status |
|---|---|---|
| 0 | Repo scaffold (`src/app/`, config, deps) | Done |
| 1 | Database — 7 SQLAlchemy models matching the live Prisma schema, zero-diff Alembic baseline `3169311c48d2` | Done |
| 2 | Auth — Kinde JWT via JWKS, read-only `current_user` | Done |
| 3 | Checkpointing — `AsyncPostgresSaver` on the same DB | Done |
| 4 | Core routes — chats CRUD, messages, approve | Done |
| 5 | LangGraph Plan→Confirm→Build→Validate flow, approval interrupt, validated end-to-end | Done |
| 6 | Streaming — AI SDK v7 **Data Stream Protocol** (UI Message Stream) | Done |
| 7 | Sandbox/files — per-turn ephemeral scratch, workflow persisted to `Message.meta` | Next |
| 8 | Tests — coverage hardening | Not started |
| 9 | Containerize + CI (Dockerfile, GitHub Actions) | Not started |
| 10 | First deploy (Railway) | Deferred — owner runs it |

The build checklist, settled architecture decisions, and phase-level implementation
records are the source of truth — see [Source-of-truth docs](#source-of-truth-docs).

## Quickstart

### Prerequisites

- **Python 3.14.6** (pinned in `.python-version`) and [`uv`](https://docs.astral.sh/uv/).
- A **`.env`** file. Copy `.env.example`, then fill in real values — at minimum
  `DATABASE_URL`, `KINDE_ISSUER_URL`, `OLLAMA_API_KEY`. `.env` is gitignored;
  **never commit it.**

```dotenv
# Required
DATABASE_URL=postgresql://user:password@host.neon.tech/neondb?sslmode=require   # direct/unpooled
KINDE_ISSUER_URL=https://your-app.kinde.com
OLLAMA_API_KEY=

# Optional / when needed
KINDE_AUDIENCE=
N8N_API_URL=        # only for n8n-mcp's 16 management tools
N8N_API_KEY=
CORS_ORIGINS=http://localhost:3000
```

> The backend must use the **direct (unpooled)** Neon URL — never the pooled `DATABASE_URL`
> the frontend uses.

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

The suite hits a live Postgres database (the checkpointer test creates and drops the
LangGraph tables), so `.env` must be present and `DATABASE_URL` pointed at the
**`tests` Neon branch**, never production data.

```sh
uv run pytest                    # whole suite (64 tests)
uv run pytest -k streaming       # a single area
uv run pytest tests/test_security.py
```

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
| GET/PATCH/DELETE | `/chats/{chat_id}` | read / rename / soft-delete |
| GET | `/chats/{chat_id}/messages` | history (resume) |
| POST | `/chats/{chat_id}/messages` | send message → streaming reply |
| POST | `/chats/{chat_id}/approve` | resume interrupted graph (approved + feedback) |
| GET | `/chats/{chat_id}/messages/{message_id}/attachments/{attachment_id}/download` | workflow file download |
| GET | `/credits` | stub (balance) |
| POST | `/webhooks/*` | stub (payments — Razorpay later) |

### Streaming contract

Message/approve routes return SSE in the AI SDK v7 **Data Stream Protocol** (UI Message
Stream): `start` → transient `data-status` progress heartbeats → `text-start` /
`text-delta*` / `text-end` (fresh uuid part id per text segment) → `finish` →
`[DONE]`, with header `x-vercel-ai-ui-message-stream: v1`. `useChat()` in the frontend
consumes it with zero config. Full wire spec: `_markdown/backend-api-schema.md` §5.

## Repo map

```
├── src/app/                  production backend (FastAPI)
│   ├── main.py               app factory, lifespan, routers
│   ├── config.py             pydantic-settings (env-driven)
│   ├── db.py                 async engine + session factory
│   ├── deps.py               current_user, pagination, ownership
│   ├── api/v1/               chats.py, messages.py, credits.py, webhooks.py
│   ├── models/               SQLAlchemy models (match contract.prisma)
│   ├── schemas/              request/response DTOs
│   ├── services/             agent.py (LangGraph), llm.py, checkpoint.py, billing.py
│   ├── core/                 security.py (Kinde JWKS), exceptions.py
│   ├── streaming.py          Data Stream Protocol encoder
│   └── prompts/              plan/build/repair + guardrails
├── tests/                    pytest suite (64 tests)
├── alembic/                  migrations (zero-diff baseline only)
├── _markdown/                SOURCE OF TRUTH — checklist, settled decisions, plans, records
├── prototype/                working single-agent prototype (terminal UI, SQLite)
├── prompts/                  prototype-only prompt placeholders
└── notebooks/                prototyping + testcase prompts
```

## Source-of-truth docs

Read in this order — they define what's built and why; don't decide divergences silently:

1. `_markdown/python-fastapi-backendchecklist.md` — the 10-phase checklist + folder structure.
2. `_markdown/backend-setup-qa.md` — settled architecture decisions (Q1–Q10).
3. `_nextjs_repo_context/prisma/contract.prisma` — the **live Neon schema** this backend mirrors.
4. `_markdown/backend-api-schema.md` — the API + streaming contract the frontend codes against.

Implementation records per phase: `_markdown/phase5-implementation.md`,
`_markdown/phase6-implementation.md`, `_markdown/phase6/` (plans + wire spec).

## Guardrails

- Never fabricate node types, parameters, credentials, or endpoints — ground via MCP lookups.
- Never write real secrets into workflow JSON — empty credential placeholders only.
- Never call a workflow "done" without passing validation **and** explicit human approval.
- Schema validity ≠ logical correctness — re-read spec vs. workflow before delivery.
- `users` is Prisma/Next.js-owned; the backend is a **read-only consumer** (401 if the
  User row is missing — a real flow error, never papered over).