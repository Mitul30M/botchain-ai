# Python/FastAPI Backend — Build Checklist

Builds on the existing `botchain-ai` repo (currently just `prototype.py` +
planning docs — no FastAPI/DB/Docker yet). Ships incrementally to a first
working Railway deployment, staying on Ollama (per current decision) until
that's stable.

## Phase 0 — Repo scaffold
- [x] Add deps to `pyproject.toml`: `fastapi`, `uvicorn[standard]`,
      `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic-settings`,
      `langgraph-checkpoint-postgres`, `python-jose[cryptography]` (Kinde JWT
      verification) — keep existing `langgraph`, `langchain-mcp-adapters`,
      `langchain-ollama`, `deepagents`, `rich`
- [x] Build out `src/app/` per the agreed structure (models/, schemas/,
      api/v1/, services/, core/) — see the answers doc for what changes
      from the original plan (Kinde not NextAuth, checkpoint service added)
- [x] `.env.example`: `DATABASE_URL` (direct, not pooled — see Q9),
      `KINDE_ISSUER_URL`, `OLLAMA_API_KEY`, `N8N_API_URL`, `N8N_API_KEY`,
      `CORS_ORIGINS`
- [x] Fix the `os.environ["X"] = os.getenv("X")` None-crash risk from
      `prototype.py` while porting it over (flagged in the code review)

botchain-ai/
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example
├── Dockerfile
├── .dockerignore
├── alembic.ini
├── README.md
│
├── alembic/
│ ├── env.py
│ └── versions/
│
├── src/
│ └── app/
│ ├── __init__.py
│ ├── main.py # FastAPI() instance, lifespan, router mounts, CORS
│ ├── config.py # pydantic-settings: env vars (DB url, API keys, secrets)
│ ├── db.py # async engine + session factory + get_session dep
│ ├── deps.py # shared Depends: current_user, wallet balance check, pagination
│ │
│ ├── models/ # SQLModel table= True classes (from schema.py, split up)
│ │ ├── __init__.py
│ │ ├── chat.py # Chat
│ │ ├── message.py # Message, Attachment
│ │ └── billing.py # CreditWallet, CreditTransaction, PaymentTopup
│ │
│ ├── schemas/ # Pydantic request/response DTOs (API-facing, not DB-facing)
│ │ ├── chat.py
│ │ ├── message.py
│ │ └── billing.py
│ │
│ ├── api/
│ │ ├── __init__.py
│ │ ├── router.py # aggregates all v1 routers
│ │ └── v1/
│ │ ├── chats.py # CRUD: list/create/rename/delete chats
│ │ ├── messages.py # POST message -> SSE streaming response
│ │ ├── credits.py # GET balance, GET ledger history
│ │ └── webhooks.py # Razorpay/Stripe payment webhooks
│ │
│ ├── services/ # business logic, framework-agnostic
│ │ ├── llm.py # Anthropic client wrapper, streaming, retries
│ │ ├── context.py # context-window packing + rolling summarization
│ │ ├── billing.py # cost calc, ledger writes, wallet debit/credit (atomic)
│ │ └── payments/
│ │ ├── razorpay.py # create order, verify webhook signature
│ │ └── stripe.py # (added later, if/when needed)
│ │
│ └── core/
│ ├── security.py # verifies the NextAuth session/JWT on each request
│ └── exceptions.py # domain exceptions -> HTTP error mapping
│
├── tests/
│ ├── conftest.py # test DB fixture (spins up against a Neon branch or sqlite)
│ ├── test_chats.py
│ ├── test_messages.py
│ └── test_billing.py
│
└── .github/
└── workflows/
└── ci.yml # lint + test + docker build (+ deploy on main)

## Phase 1 — Database (SQLAlchemy + Alembic against Neon)
- [x] Write SQLAlchemy models for `User`, `Chat`, `Message`, `Attachment`,
      `CreditWallet`, `CreditTransaction`, `PaymentTopup` — same table and
      column names as `contract.prisma` (already snake_case via `@map`)
- [x] `alembic init`, wire `env.py` to read `DATABASE_URL` from `config.py`
- [x] **Baseline, don't recreate**: these tables already exist (Prisma made
      them). Run `alembic revision --autogenerate` against the live Neon DB
      — it should produce an empty or near-empty diff since the tables
      already match — then `alembic stamp head` instead of `upgrade head`.
      Confirm the diff is actually empty before stamping; a mismatch here
      means the SQLAlchemy models don't line up with Prisma's tables yet.
- [x] From here on, every future schema change to these tables goes through
      Alembic (backend now owns migrations for everything except `users`,
      which Next.js/Prisma still owns)

## Phase 2 — Auth (Kinde JWT verification)
- [x] `core/security.py`: fetch Kinde's JWKS (`{KINDE_ISSUER_URL}/.well-known/jwks.json`),
      verify incoming bearer tokens against it (issuer + audience + expiry)
- [x] `deps.py`: a `current_user` dependency that takes the verified token's
      `sub` (Kinde id) and resolves it to the local `User` row (same
      kinde_id → id lookup Next.js already does in its layout) — cache this
      per-request, don't re-query it twice in one handler
- [x] Decide now how the token reaches FastAPI: via the Next.js proxy route
      (Phase 5) attaching it server-side is the recommended path — see Q7; proxy
      must use `getAccessTokenRaw()` not `getAccessToken()` (see backend-setup-qa.md Q7)

## Phase 3 — Agent persistence (checkpointing + context)
- [x] Replace `AsyncSqliteSaver` with `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`,
      pointed at the same Neon `DATABASE_URL`
- [x] Call `await checkpointer.setup()` once in the FastAPI lifespan startup
      — this creates LangGraph's own checkpoint tables automatically; they
      are **not** part of your Alembic-managed schema (see Q1)
- [x] Use `Chat.context_summary` (already in the schema) for the rolling
      conversation summary — no new table needed for that half of "memory"
- [x] Move `agent`/`model`/`mcp_client` out of module-level globals into
      FastAPI's `app.state`, set up once in the lifespan, not per-import

## Phase 4 — Core API routes
- [x] `POST /api/v1/chats` — create a chat for the current user
- [x] `GET /api/v1/chats` — list current user's chats
- [x] `GET/PATCH/DELETE /api/v1/chats/{chat_id}`
- [x] `POST /api/v1/chats/{chat_id}/messages` — send a message, streaming
      response (see Q7 for the exact protocol); stub echo agent for now
- [x] `GET /api/v1/chats/{chat_id}/messages` — full history (for resume)
- [x] `POST /api/v1/chats/{chat_id}/approve` — stub: persists the
      approve/reject decision on the pending assistant message (409 if none);
      real graph resume comes in Phase 5
- [x] Leave `credits.py` / `webhooks.py` as empty stub routers for now —
      per Q4, no enforcement or wiring yet

### Phase 4 notes (verified 2026-09-22, live smoke)
- Persistence semantics: the **user** Message row is committed *before*
  streaming starts; the **assistant** row is written once by the streaming
  generator via its own session (the request-scoped `Depends(get_session)`
  session is torn down when the handler returns). On client disconnect uvicorn
  cancels the request task, so a `finally`-block `await` would never commit —
  the final write is handed to a **strongly-referenced fire-and-forget task**
  (`_fire_and_forget` keeps the task in a module-level set until it finishes,
  and logs any failure) so the partial reply still persists with
  `is_error=True`. Verified three times against live Neon with interrupted
  `curl --max-time 1`; covered by `tests/test_message_flush.py`.
- `GET /chats` orders by `created_at DESC`; messages by `created_at ASC`.
  Rename (`PATCH`) accepts `title` only; there is deliberately **no**
  `updated_at` bump on message writes (matches Next.js expectations).
- Ownership/missing chat both return **404** (`get_owned_chat`); there is no
  separate 403.
- `meta` footgun (fixed): Prisma created the live `chats`/`messages`.`meta`
  columns with default `'"{}"'::json` — a **JSON string**, not an object. The
  SQLAlchemy models now use `default=dict` (Python-side, so inserts always
  bind a real JSON object) plus `server_default=text("'\"{}\"'::json")` to
  stay byte-identical with the live DDL. Do not revert to `'{}'::json` —
  autogenerate would flag the mismatch.
- Note for the running pattern: `.env` lives at repo root, so uvicorn must be
  started from the repo root with `--app-dir src`
  (`uv run uvicorn app.main:app --app-dir src --port 8000`), not from `src/`.

## Phase 5 — Wire the LangGraph flow from `botchain-ai`'s own plan
- [x] Port the already-designed Plan → Confirm → Build → Validate graph
      (from the backend repo's own `README.md`/`AGENTS.md`) into
      `services/agent.py`, using `Chat`/`Message` rows instead of a
      separate "session" concept
- [x] `request_human_approval` (from `prototype.py`) becomes the Confirm/
      approval-gate node, surfaced through the `/approve` route above
      instead of a terminal prompt

### Phase 5 notes (verified 2026-09-24, live graph + concurrent smoke)
- Graph in `services/agent.py`: START → plan ⇄ (until spec complete) →
  confirm (`interrupt()`) → build (LLM + n8n-mcp tools + `write_json_file`
  into a per-build `tempfile` sandbox) → validate ⇄ build (self-repair, ≤3) → END.
  `AgentState` keys: `messages`, `phase`, `spec`, `workflow_json`,
  `workflow_name`, `validation_status`, `validation_errors`, `retry_count`,
  `build_feedback`, `confirm_summary` (all optional; merges across re-invokes).
- Structured output footgun: Ollama Cloud's `json_schema` structured-output
  mode does **not** enforce JSON (`nemotron-3-ultra:cloud` opts out). Must use
  `with_structured_output(..., method="function_calling")`, and even then the
  model occasionally returns `None`/prose — `_invoke_structured_with_retry`
  escalates: structured attempt → retry with an explicit JSON directive that
  embeds the `PLAN_SCHEMA` → plain-completion fallback parsed from raw JSON.
  Covered by `tests/test_plan_fallback.py` (5 tests).
- Persisted-reply footgun: `stream_mode="messages"` also surfaces **internal**
  model calls (build/validate repair passes emit the raw workflow JSON as
  streamed tokens). The assistant row must come from the final committed
  state message (`_final_assistant_text`), never from joining the stream
  buffer. Covered by `tests/test_message_persistence.py`.
- Verified end-to-end against the real stack: the Easy testcase prompt
  ("contact form … post to #general Slack") ran plan → confirm (approval
  interrupt) → approve via `/approve`-equivalent `Command(resume=…)` →
  build → validate (passed n8n validation after self-repair) → `phase=done`,
  `validation.status=valid`, `workflow_json` = 2-node Webhook + Slack workflow.
- Concurrent-build smoke (delta 1): two chats streaming simultaneously stay
  isolated — no stream cross-contamination, each chat persists only its own
  workflow in meta. Per-chat `_chat_locks` serialise same-chat runs.
- Status heartbeats stream as custom `{"status": …}` events so the connection
  stays alive during long build/validate passes; the per-chat lock is released
  in the generator's `finally` (the request task is gone by then).
- Dead code / n8n-mcp notes: `MultiServerMCPClient.__aexit__` raises
  NotImplementedError → shutdown just nulls `app.state.mcp_client` (each MCP
  tool call spawns its own npx stdio subprocess, so there is nothing to close).
  Build binds only the curated `BUILD_TOOL_NAMES` allowlist (7 core
  search/get/validate/template tools); management tools (`n8n_*`) are
  deliberately excluded and need `N8N_API_URL`/`N8N_API_KEY` + a live n8n
  instance.
- Streamed text vs persisted text: the client still *sees* internal tool/JSON
  fragments in the raw token stream; only the persisted row is cleaned. Tabs
  with the Data Stream Protocol later if tool-call indicators are wanted.
- **Prompts modularized (2026-09-25)**: agent prompts moved out of
  `services/agent.py` into a `src/app/prompts/` package (ships in the wheel via
  `packages = ["src/app"]`): `plan.py`, `build.py`, `repair.py`, `constants.py`.
  Root `prompts/` stays a prototype-only placeholder (single-agent prompt,
  references tools that don't exist in prod); `prototype.py` still imports it.
  Guardrails §7–§10 (redirect off-topic / decline harmful / no unrequested
  nodes / show file on request) + the secrets-warning now load into the model
  prompts, not just AGENTS.md.
- **Confirm-flush precedence bug (found in HTTP e2e, fixed 2026-09-25)**: the
  `context_summary` branch of `_flush_assistant_row` was
  `await session.execute(...).scalar_one_or_none()` — `await` bound to the
  **result** of `.scalar_one_or_none()`, so `execute()`'s coroutine got the
  attribute. It only triggers on `meta.phase == "confirm"` (updates Chat row),
  so Phase 4's mock agent never hit it; the first real confirm turn crashed the
  flush, dropped the pending-approval row, and skipped `lock.release()` (chat
  wedged open). Fixed to `(await session.execute(...)).scalar_one_or_none()`
  and both stream-generator `finally` blocks now wrap the flush in a nested
  `try/finally` so the lock is always released. Regression tests added to
  `tests/test_message_persistence.py` (2). Ruff + 34 pytest green.
- **Real HTTP + Neon DB-persistence e2e (2026-09-25)**: full stack via routes —
  chat `db-persistence-test-ollama-model-e2e`, Kinde JWT, uvicorn + Ollama +
  n8n-mcp. Three planner turns (trigger/field-name questions answered), confirm
  summary, `/approve`, build + validate passed. Exactly 7 `messages` rows
  persisted: 3 user (no meta), 2 plan (`phase=plan`), 1 confirm
  (`phase=confirm`, `approval.status=approved` after `/approve`, spec in meta),
  1 done (`phase=done`, `validation.status=valid`, `workflow_json` +
  `workflow_name`). Chat row carries `context_summary` from the confirm turn.
  Verified live in Neon (`billowing-snow-05570527`, production branch).

## Phase 6 — Streaming to Next.js
- [x] Have the message-send route return a streaming response. **Decision
      (2026-09-26): standardized on the AI SDK v7 Data Stream Protocol / UI
      Message Stream (`x-vercel-ai-ui-message-stream: v1`), not the
      Text-Stream-Protocol-first wording below — see
      `_markdown/phase6/backend-phase6-plan.md` §Decision. Text Stream
      Protocol stays only as a future option for `/completion`-style
      endpoints that need no status data.
- [x] Confirm the Next.js proxy route (Phase 5 of the frontend work) pipes
      this straight through without buffering — `_markdown/phase6/nextjs-app-phase6-plan.md`.

## Phase 7 — Sandbox / workflow draft handling
- [ ] Don't rely on `SANDBOX_DIR` surviving between requests or replicas —
      see Q8. For now: single Railway replica, sandbox as scratch space
      only, final workflow JSON persisted to Postgres (e.g. in
      `Message.meta` or a small dedicated table) at the end of each turn

## Phase 8 — Tests
- [ ] `conftest.py`: a Neon branch (or local Postgres in CI) as the test DB
- [ ] Unit tests for the approval-state transitions and the
      `_extract_json_from_mcp_result`/`_strip_code_fences` helpers as pure
      functions, independent of the live MCP subprocess
- [ ] One smoke test per route

## Phase 9 — Containerize + CI/CD
- [ ] Multi-stage `Dockerfile` (uv install → slim runtime)
- [ ] `.github/workflows/ci.yml`: lint + test + `docker build` on every PR
- [ ] Deploy step on `main`: build, push to GHCR (or let Railway build
      directly from the Dockerfile), Railway CLI/GitHub Action to deploy
- [ ] Railway: single service, single replica to start (matches the
      sandbox constraint in Phase 7), env vars set in Railway's dashboard,
      region matched to the Neon project's region

## Phase 10 — First deployment
- [ ] Deploy, confirm Ollama Cloud is reachable from Railway's network
- [ ] End-to-end smoke test: Next.js → proxy route → FastAPI → stream back
      → rendered in `useChat`
- [ ] Treat the app as free-to-use at this point — no credit checks yet,
      per Q4
