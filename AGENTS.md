# BotChain AI — Backend (Python/FastAPI)

## What this project is
BotChain AI lets a business user describe a problem or process in plain language and
receive back a ready-to-import n8n automation — no knowledge of nodes, APIs, or JSON
required. A two-phase conversational agent (Plan → Build) interviews the user into a
structured requirements spec, then generates a validated n8n workflow JSON file,
grounding every node-level decision in live n8n documentation via the n8n-mcp server
(1,600+ nodes) rather than model memory, and validating the result programmatically
before it reaches the user.

This repo is the **Python/FastAPI backend**. It is being brought from a single working
prototype (`prototype/prototype.py`) to a production-ready API. The Next.js frontend
(`botchain-ai-next-app`, a separate repo) is already underway — Kinde auth wired, and the
Prisma + Neon Postgres schema live in production. Backend↔frontend wiring (streaming
routes through a Next.js proxy) happens only after this backend is in good shape.

## Repo map & current state
```
botchain-ai/
├── prototype/prototype.py      # working single-agent prototype (terminal UI, SQLite checkpoints)
├── prompts/                    # agent system prompt + the curated ~15-node surface
├── notebooks/                  # prototyping notebook + testcase prompts (eval fixtures source)
├── _markdown/                  # SOURCE OF TRUTH: build checklist + settled decisions
├── _nextjs_repo_context/       # reference files from the Next.js repo (contract.prisma, etc.)
├── src/app/                    # production backend (being built — see milestone plan)
├── tests/                      # pytest suite (being built)
└── pyproject.toml              # uv-managed
```
Status: **production build in progress — working through the checklist in `_markdown/`
phase by phase.** Phase 1 (database) done — 7 SQLAlchemy models match the live Prisma
schema with a zero-diff Alembic baseline (`3169311c48d2` stamped);
Phase 2 (auth) done — Kinde JWKS verification + read-only `current_user`;
Phase 3 (checkpointing) done — `AsyncPostgresSaver` on pool, Alembic exclusion verified;
next: Phase 4 (core API routes).

## Read first — source of truth (in this order)
1. `_markdown/python-fastapi-backendchecklist.md` — the 10-phase build checklist AND the
   confirmed `src/app` folder structure. This defines what to build and in what order;
   do not invent your own sequencing or skip ahead before a phase is verified working.
2. `_markdown/backend-setup-qa.md` — settled architecture decisions (Q1–Q10).
3. `_nextjs_repo_context/prisma/contract.prisma` (+ `contract.json`) — the **live Neon
   schema** this backend connects to. Read before touching anything database-related.
   Also: `_nextjs_repo_context/AGENTS.md` + `nextjs-app-checklist.md` for frontend context.
4. If anything in the checklist is ambiguous, or work needs to diverge from it — **stop
   and ask**, don't decide silently.

## Non-negotiable database rules
- SQLAlchemy models must match `contract.prisma` **exactly** — same table names, same
  columns (already snake_case via `@map`). Do not rename, add, or drop columns on the
  tables Prisma owns.
- `users` stays **Prisma/Next.js-owned**. The backend is a **read-only consumer**: it
  looks the thin User row up by `kinde_id` on auth and returns 401 if it's missing
  (means the sync/registration didn't run — a real flow error, not something FastAPI
  should paper over). FastAPI must never create, migrate, or alter that table. User
  creation lives in one place only: the Next.js `/api/auth/sync` route. This avoids
  the SELECT-then-INSERT TOCTOU race of two services both doing "check then insert"
  on the same `kinde_id`-unique row.
- **Baseline, don't recreate.** The tables already exist in production (Prisma created
  them). Run `alembic revision --autogenerate`, verify an empty/near-empty diff, then
  `alembic stamp head` — never `upgrade head` to recreate them.
- LangGraph checkpoint tables are LangGraph-owned (created by `checkpointer.setup()` in
  the lifespan) — not part of the Alembic-managed schema.
- Never hand-edit tables in the Neon console.
- Live DB: Neon project `botchain-ai` (`billowing-snow-05570527`), branch `production`
  (br-aged-sunset-b39a2u2u), database `neondb`, `public` schema.

## Production build plan (milestones)
Each phase must be **verified working** before the next begins.

| # | Milestone | Deliverables | Verification gate | Status |
|---|---|---|---|---|
| 0 | Repo scaffold | `src/app/` skeleton per checklist; deps (fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic, pydantic-settings, langgraph-checkpoint-postgres, python-jose; drop aiosqlite/sqlite-checkpointer); `config.py` via pydantic-settings (fixes prototype's `os.environ` None-crash); `.env.example` | App imports and boots | Done (import + /health 200 + ruff clean) |
| 1 | Database | 7 SQLAlchemy 2.0 async models matching contract.prisma; alembic wired to direct `DATABASE_URL`; empty autogen diff → `stamp head` | Empty diff committed; app queries live DB | Done (zero-diff baseline `3169311c48d2` stamped; live ORM + psycopg/asyncpg both verified) |
| 2 | Auth | `core/security.py` Kinde JWT verification via JWKS (`<issuer>/.well-known/jwks.json`, cached); `deps.py` `current_user` (sub→User **read-only**, per-request cache, **401 if no User row** — no get-or-create) | Test JWT passes/fails against stub JWKS | Done (18 pytest cases green: minted RS256 JWTs vs mocked JWKS + read-only current_user; live JWKS fetch OK) |
| 3 | Checkpointing | `services/checkpoint.py` `AsyncPostgresSaver` (same `DATABASE_URL`); `setup()` once in lifespan; agent/model/mcp_client into `app.state` (no module globals) | LangGraph checkpoint tables appear in Neon | Done (pool-based factory, 4 tables in Neon, Alembic exclusion verified) |
| 4 | Core routes | `/api/v1/chats` CRUD (soft-delete), `GET/POST messages`, `POST /approve`; `credits.py` + `webhooks.py` empty stubs | Curl smoke per route (mock agent) | Done (all routes smoky green against live Neon: create/list/get/rename, happy+disconnect streams (is_error row persisted), approve 409+round-trip, 404s, soft-delete; ruff + 23 pytest green; disconnect flush is a strongly-referenced fire-and-forget task with logged failures) |
| 5 | LangGraph flow | Port Plan→Confirm→Build→Validate StateGraph into `services/agent.py`; approval interrupt resumed via `/approve` (replaces terminal `input()`); helpers ported; n8n-mcp stdio tools + node-lookup cache | A `notebooks/testcases.md` prompt runs end-to-end → validated workflow | Not started |
| 6 | Streaming | Message route returns StreamingResponse (Vercel AI SDK **Text Stream Protocol**, plain chunks) | Incremental tokens over curl | Not started |
| 7 | Sandbox/files | Per-turn `tempfile` sandbox (ephemeral — Railway disk doesn't survive); final workflow JSON persisted to `Message.meta` | Workflow survives request via DB, not disk | Not started |
| 8 | Tests | `conftest.py` on `tests` Neon branch; unit tests (helpers, approval transitions, auth, spec-completeness); one smoke per route; graph-fixture with fake n8n tools | `pytest` green | Not started |
| 9 | Containerize + CI | Multi-stage `Dockerfile` (uv build → slim runtime, node for `npx n8n-mcp`); `.github/workflows/ci.yml` (ruff + pytest + docker build on PR; guarded deploy on main) | `docker build` passes locally | Not started |
| 10 | First deploy (deferred) | Railway deploy + e2e smoke | — | Out of current scope — user runs it |

## Settled decisions (do not re-litigate without a reason)
- **ORM:** SQLAlchemy 2.0 typed async models (NOT SQLModel).
- **LLM:** Ollama (`ChatOllama`, cloud `nemotron-3-ultra:cloud`) for now, **behind
  `services/llm.py`** so swapping to Claude later touches one file only.
- **Auth:** Kinde JWT via JWKS (`python-jose`); token arrives via the Next.js proxy route
  (frontend-phase wiring), CORS is a non-issue if that proxy pattern holds.
- **`current_user`:** read-only `kinde_id` → User lookup. No get-or-create. A missing
  User row → **401** ("registration not complete"); the Next.js `/api/auth/sync` route
  is the single owner of user creation, so FastAPI never races it.
- **DB connection:** direct (non-pooled) `DATABASE_URL` for the app and Alembic.
- **Tests DB:** a dedicated `tests` Neon branch (stamped head), never the production data.
- **Checkpointer:** `AsyncPostgresSaver` on the same `DATABASE_URL`, `setup()` at startup;
  conversation memory uses `Chat.context_summary` (no extra table).
- **Approval gate:** LangGraph `interrupt()` surfaced through `POST /api/v1/chats/{id}/approve`;
  UI approval actions come in the frontend phase.
- **Streaming:** Text Stream Protocol first (plain incremental text); upgrade to the Data
  Stream Protocol later for tool-call/reasoning indicators.
- **Sandbox/state:** single Railway replica; sandbox is per-turn scratch; workflow JSON
  persisted to `Message.meta` at the end of each build.
- **Billing:** `credits.py`/`webhooks.py` are empty stubs — no balance checks, deductions,
  or payment calls yet. Treat every request as free during this phase.
- **n8n-mcp:** self-hosted stdio (`npx n8n-mcp`) with env injection; core tools work with
  no n8n instance; cache `search_nodes`/`get_node` results in-memory. Use the real tool
  names (see Frontend context below) — there is no `get_node_essentials` tool.
- **Model choice:** curated ~15-node surface (Webhook, HTTP Request, Set, IF/Switch, Code,
  Schedule Trigger, Gmail, Slack, Google Sheets, Telegram…) preferred in the plan prompt.
- **LangSmith:** tracing controlled by env vars; off by default in prod unless explicitly set.

## Architecture

### Folder structure (from the checklist — stay in it)
Under a `src/` root so the app package is `src/app/`:
```
app/
├── main.py        # FastAPI instance, lifespan, router mounts, CORS
├── config.py      # pydantic-settings: DATABASE_URL, KINDE_ISSUER_URL, keys
├── db.py          # async engine + session factory + get_session dep
├── deps.py        # shared Depends: current_user, pagination, ownership
├── models/        # chat.py, message.py, billing.py (+ __init__.py)
├── schemas/       # Pydantic request/response DTOs (chat/message/billing)
├── api/           # router.py aggregates; v1/: chats.py, messages.py, credits.py, webhooks.py
├── services/      # llm.py, context.py, billing.py, agent.py, checkpoint.py, payments/
└── core/          # security.py, exceptions.py
```

### Core state design
`RequirementsSpec` (goal, trigger_type, services_involved, conditions_logic, data_flow,
constraints, open_questions) + `AgentState` (messages, phase, spec, workflow_json,
validation_errors, retry_count). `phase` is the mode switch the frontend reads to show
"Planning…" vs "Building…" and is what routes the LangGraph edges.

### Graph flow
```
START → plan_node ⇄ (loops with the user until the spec is complete & no open questions)
              ↓
        confirm_node (interrupt: plain-English spec summary — build it?)
              ↓ (user confirms via /approve)
        build_node → validate_node → (pass) → END
                          ↓ (fail, retry_count < 3)
                     build_node (self-correct on validation errors) → surface gracefully if not converged
```
- plan_node: chat loop that fills `RequirementsSpec` via structured output each turn.
- confirm_node: `request_human_approval` port — LangGraph interrupt resumed via `/approve`.
- build_node: LLM with n8n-mcp tools bound + `write_json_file`; assemble workflow JSON.
- validate_node: `build_workflow_with_validation` (validate → self-repair, ≤3 retries);
  final validated JSON persisted to `Message.meta`.

### API surface (v1, user-scoped)
```
POST   /api/v1/chats                          create chat
GET    /api/v1/chats                          list user's chats
GET/PATCH/DELETE /api/v1/chats/{chat_id}      read / rename / soft-delete
GET    /api/v1/chats/{chat_id}/messages       history (resume)
POST   /api/v1/chats/{chat_id}/messages       send message → streaming text response
POST   /api/v1/chats/{chat_id}/approve        resume interrupted graph (approved + feedback)
GET    /api/v1/credits                        stub (balance)
POST   /api/v1/webhooks/...                    stub (payments — Razorpay later)
```

## Guardrails (inherited from the system prompt — keep them)
- Never fabricate a node type, parameter, credential field, or API endpoint — ground via
  MCP lookups; only include properties actually retrieved.
- Never write real secrets/keys/tokens into workflow JSON — empty credential placeholders
  only; warn the user if they share live secrets in chat.
- Never call a workflow "done" without passing validation AND explicit human approval.
- Cap validation retries at 3; on non-convergence deliver best-effort JSON + a clear list
  of remaining issues and manual-fix instructions — never silently fail.
- Schema validity ≠ logical correctness: the build loop also re-reads spec vs. workflow
  to flag mismatches before delivery.

## Conventions & tooling
- **uv** for everything: `uv sync`, `uv add`, `uv run`. Python pinned via `.python-version`
  (3.14).
- Lint with ruff; tests with pytest + pytest-asyncio. No code comments unless necessary.
- `.env` is gitignored. `.env.example` holds placeholders only. Never commit secrets.
- ALWAYS check current phase progress before starting; work the checklist in order and
  confirm each phase works before moving on. Ambiguity → stop and ask the user.

## Running / developing
1. `uv sync` (installs from `pyproject.toml` + `uv.lock`).
2. `.env` — required: `DATABASE_URL` (direct, not pooled), `KINDE_ISSUER_URL` (+ optional
   `KINDE_AUDIENCE`), `OLLAMA_API_KEY`, `N8N_API_URL`, `N8N_API_KEY`, `CORS_ORIGINS`,
   `LANGSMITH_*` (tracing off by default).
3. Run API: `uv run uvicorn app.main:app --reload` (reload optional).
4. Alembic: `uv run alembic revision --autogenerate -m "..."` → confirm diff → `uv run
   alembic upgrade head` (or `stamp head` for the Prisma-created baseline).
5. Tests: `uv run pytest` (uses the `tests` Neon branch).
6. End-to-end agent smoke (Phase 5+): feed a prompt from `notebooks/testcases.md` through
   `POST /messages` with Ollama + n8n-mcp running.

## External resources
- Neon (DB): project `botchain-ai` (`billowing-snow-05570527`), branch `production`
  (br-aged-sunset-b39a2u2u), db `neondb`. `tests` branch reserved for test runs.
- Kinde: issuer URL supplied by the repo owner; JWKS at `<issuer>/.well-known/jwks.json`.
- Ollama Cloud: `OLLAMA_API_KEY` (cloud-hosted model, base URL https://ollama.com).
- n8n-mcp: via `npx n8n-mcp` (stdio, self-hosted). Core tools (search/get/validate)
  need no n8n instance; the management tools need `N8N_API_URL` + `N8N_API_KEY`.

## Frontend context (`botchain-ai-next-app`, separate repo)
Facts captured by exploring the Next.js repo — keep them in mind so the two sides meet
without surprises at wiring time (frontend↔backend wiring is frontend-phase work, done
only after this backend repo is in good shape).

- Frontend: Next.js 16 App Router, **Kinde hosted auth**, Prisma 8 contract mode → Neon;
  `pnpm` only (ours is `uv`). It is at its own Phase 3 (chat UI shell — mock cards and
  skeletons, **no API calls yet**). Its live `src/prisma/contract.prisma` was verified
  byte-identical to the copy in `_nextjs_repo_context/`.
- User-facing identifier is the **local `User.id`** (Postgres UUID), not `kindeId`.
  Frontend routes are `/users/{User.id}/…` and its layout resolves `kindeId` → local
  User (redirects on mismatch). Use `User.id` in all backend payloads.
- User creation lives only in the frontend `/api/auth/sync` route (creates User +
  CreditWallet 5.00 + CreditTransaction `signup_grant` in one transaction). Backend is a
  read-only consumer — see DB rules above. Env names: frontend `DATABASE_URL` = pooled,
  `DIRECT_URL` = unpooled; the backend must use the **direct** URL.
- Real n8n-mcp tools (7 core, no n8n instance needed): `tools_documentation`,
  `search_nodes`, `get_node` (mode `essentials`/`full`/…), `validate_node`,
  `validate_workflow`, `search_templates`, `get_template`; 16 management tools need
  `N8N_API_URL`/`N8N_API_KEY`. Use these real names — there is no `get_node_essentials`.
- Approval-gate reconciliation (revisit at wiring phase): frontend planning docs wanted a
  `Message.status` "pending_approval" flag; this repo's settled decision is LangGraph
  `interrupt()` surfaced via `POST /api/v1/chats/{id}/approve`. The `Message` table has
  no `status` column, so "waiting for approval" is signaled via `Message.meta` JSON.