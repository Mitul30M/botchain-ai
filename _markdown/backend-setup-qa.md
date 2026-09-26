# Backend Setup — Answers

## 0. Note regarding context from the other repo

- I'll keep the context files required for some task in the @_context folder in root, so like eg. the contract.prisma file used in the Nextjs repo for the Neon hosted Postgres already created the the models and i want to access those tables/db in this repo with sql alchemy such tht i dnt overwrite the existing model schema but create new tables when required and access and query any table from tht db. similarly i used kinde auth in the nextjs app repo (which holds the ui logic using vercel ai sdk) so ill need to verify the jwts as mentioned in @python-fastapi-backendchecklist.md. This file contains what my plan is but actual checklist holds the phasesa nd milestones to be achived thts our road to follow
 

## 1. Checkpointing & context tables — what goes where

Two different things were bundled in "checkpoints and context," and they get
different answers:

- **Agent execution state (checkpoints)** — the direct analog to
  `prototype.py`'s `AsyncSqliteSaver`. Use
  `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver` pointed at the same
  Neon `DATABASE_URL`, and call `await checkpointer.setup()` once at
  startup. This is a real, current LangGraph package — it **creates and
  manages its own tables automatically** (checkpoints, checkpoint writes,
  checkpoint blobs). Don't hand-roll SQLAlchemy models for these, and don't
  put them under Alembic — they're a separate, LangGraph-owned migration
  path in the same physical database. It's a drop-in swap for the SQLite
  saver you already have.
- **Conversation memory/context** — you already have this: `Chat.context_summary`
  in the schema is exactly the rolling-summary field for this. No new table
  needed; the checkpointer above handles turn-by-turn resumability, this
  field handles long-conversation compression.

## 2. Project structure — confirmed, two small updates

The structure you listed stands. Two changes given decisions made since:
- `core/security.py` → verifies the **Kinde JWT via JWKS** (not a NextAuth
  session; you switched to Kinde). JWKS endpoint is
  `<issuer>/.well-known/jwks.json` — Kinde's docs use the `.json` suffix
  consistently; include it even though Kinde happens to serve the no-extension
  form too.
- Add `services/agent.py` (the ported `setup_agent()`/graph logic from
  `prototype.py`) and `services/checkpoint.py` (the `AsyncPostgresSaver`
  setup) — these didn't exist in the original sketch because the prototype
  hadn't been folded in yet.

`services/payments/` stays scaffolded-but-empty per Q4 — routers exist,
nothing calls out to Razorpay yet.

## 3. CI/CD, containerization, deployment — the plan, not the implementation yet

- **Dockerfile**: multi-stage, `uv sync --frozen` in a build stage, copy
  the synced venv into a slim runtime image. Same pattern discussed for
  the Next.js repo, just uv instead of pnpm.
- **GitHub Actions**: one workflow triggered on PRs (lint, test, `docker
  build` to catch breakage — don't push or deploy from a PR), a second
  triggered on merge to `main` (build, push image, deploy).
- **Deploy target**: Railway, using either Railway's native Dockerfile
  build (point it at the repo, it builds and deploys on push) or a
  GitHub Action that calls the Railway CLI — native build-on-push is less
  moving parts for now; add the explicit Action later if you need deploy
  gating (e.g. only deploy after tests pass in a separate job).
- **Secrets**: GitHub Actions secrets for anything the CI pipeline itself
  needs (test DB URL, etc.); Railway's own env var store for runtime
  secrets (Neon URL, Kinde issuer, Ollama key, n8n key) — don't duplicate
  secrets across both unless a step genuinely needs them in CI.

This is deliberately just the plan — build it out once Phase 9 of the
checklist is reached, not before.

## 4. Billing tables — kept, unenforced

Confirmed: `CreditWallet`, `CreditTransaction`, `PaymentTopup` stay in the
Neon schema (already created), but the backend does **no** balance checks,
no deductions, no gateway calls until after the first successful
deployment. Treat every request as free during this phase. This matches
what's already documented in the Next.js repo's `AGENTS.md` — nothing new
to reconcile there.

## 5. Checklist — see `python-fastapi-backendchecklist.md`

Built directly from `prototype.py` plus your own repo's `README.md`/
`AGENTS.md`, which already lays out a solid Plan → Confirm → Build →
Validate graph design with a defined `AgentState`/`RequirementsSpec` shape
and an API surface sketch. That design is sound — the checklist ports it
into FastAPI/Postgres rather than replacing it, and swaps its "session"
concept for your actual `Chat`/`Message` tables so the backend and frontend
agree on one identifier instead of two.

## 6. Ollama for now — agreed, with one guardrail

Staying on Ollama (`ChatOllama`, matching what already works in the
prototype) until after first deployment is a reasonable call — ship what's
proven, swap the model later. The one thing worth doing now rather than
later: keep the LLM call behind `services/llm.py` as a single interface
(model in, streamed tokens out) so swapping to Claude later doesn't ripple
into the routes, the billing tables, or the streaming layer — just that one
file changes.

## 7. FastAPI routes + connecting to the Next.js AI SDK

**Routes**: listed in full in the checklist (Phase 4) — `POST/GET /chats`,
`GET/PATCH/DELETE /chats/{id}`, `POST /chats/{id}/messages` (streaming),
`GET /chats/{id}/messages`, `POST /chats/{id}/approve`.

**Streaming into `useChat`** — checked this against the AI SDK's current
docs rather than assumption, since the protocol has changed shape across
SDK versions. Two supported protocols exist:
- **Data (UI Message) Stream Protocol** — the richer format (`start`,
  `text-delta`, `data-*`, `finish` parts as SSE JSON lines, requires the
  `x-vercel-ai-ui-message-stream: v1` response header). **This is what we
  standardized on for Phase 6 (2026-09-26)** — the installed AI SDK v7
  defaults `useChat` to it (zero frontend transport config; the frontend's
  dummy `/api/chat` route already speaks it), and the agent's status
  heartbeats (`Planning…`, `Assembling the workflow file…`, `Fixing
  validation errors…`) ride along as transient `data-status` parts that a
  plain-text stream cannot carry. Also the natural home for tool-call /
  reasoning indicators later. See
  `_markdown/phase6/backend-phase6-plan.md` for the wire spec.
- **Text Stream Protocol** — plain incremental text chunks, no JSON
  framing. Supported by `useChat`/`useCompletion` with
  `streamProtocol: "text"`. Kept as a future option only for
  `/completion`-style endpoints that need no status data.

Official reference: `https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol`.
Note there's an open GitHub issue on the official FastAPI example repo
(`vercel/ai/tree/main/examples/next-fastapi`) reporting the data-protocol
version doesn't work cleanly as-is. We sidestep it by hand-rolling the
small SSE encoder (`src/app/streaming.py`) against the SDK's own installed
chunk schema rather than adopting that example.

**How the connection is wired** — don't call FastAPI directly from the
browser. Put a thin proxy route in Next.js (`app/api/chat/route.ts`) that:
resolves the Kinde session server-side, forwards the request to FastAPI
with a bearer token, and pipes FastAPI's streaming `Response` straight back
to the client. `useChat` in the frontend then points `api: "/api/chat"` —
same-origin, no CORS to configure, and the backend URL/credentials never
reach the browser.

**Token hand-off shape (decided before the proxy is built):** the proxy
route must obtain the JWT with `getAccessTokenRaw()`, **not**
`getAccessToken()` when the time comes. `getAccessToken()` has documented
inconsistency across `@kinde-oss/kinde-auth-nextjs` versions — in some
versions it returns `null` or a non-JWT token object rather than the signed
string — while `getAccessTokenRaw()` reliably returns the raw signed JWT
suitable for the `Authorization: Bearer` header. `core/security.py` verifies
whatever raw JWT string arrives, so this is purely about how the proxy
produces that string; noting it here keeps it from being relearned the hard
way during the frontend wiring phase.

## 8. Sandbox folder on Railway

Railway's container filesystem is **ephemeral by default** — writes don't
survive a redeploy, and are not shared across replicas if you ever scale
horizontally. Two implications:
- Don't treat `SANDBOX_DIR` as anything but scratch space for the duration
  of a single request/turn.
- Run **a single Railway replica** for now (matches "too early" — no need
  to solve multi-replica state yet) so at least a single conversation's
  in-progress files stay consistent across turns within one container's
  lifetime.
- Before scaling beyond one replica ever becomes necessary: persist the
  in-progress workflow JSON in Postgres (or object storage) keyed by
  `chat_id`, and materialize it into a fresh `tempfile.mkdtemp()` sandbox
  at the start of each turn, writing the result back at the end. That
  removes the dependency on local disk entirely. Not needed today — just
  the thing that makes horizontal scaling possible later without a rewrite.
- Railway does offer persistent Volumes if you want disk to survive a
  redeploy sooner — but a Volume still only attaches to one service
  instance, so it solves ephemerality, not the multi-replica problem.

## 9. Alembic vs. querying Neon

These are different jobs, worth separating clearly: **Alembic manages
schema** (creating/altering tables — DDL), it does not run your app's
queries. Your actual runtime queries go through SQLAlchemy's async engine
(`db.py`) directly. Alembic just needs a connection string (the direct one,
not pooled — see the connection-pooling note from earlier) to apply
migrations, typically run manually or in CI, not from the running app.

## 10. Things to settle before writing code

- **Confirm the Alembic baseline is truly a no-op** (Phase 1 of the
  checklist) before writing a single new migration — this is the one step
  where a mismatch between Prisma's tables and your SQLAlchemy models would
  cause real damage if missed.
- **Single Railway replica for now** — a deliberate, temporary constraint
  (Q8) that should be written down somewhere so nobody "fixes" it by
  scaling up before the sandbox/state design catches up.
- **Decide the exact Kinde token hand-off** (Q7) before writing
  `core/security.py` — Next.js proxy route with a forwarded bearer token is
  the recommendation; make sure both sides agree before either is built.
- **CORS is a non-issue if you commit to the proxy pattern** — worth
  deciding now so nobody spends time configuring CORS on the FastAPI side
  for a direct-from-browser call that shouldn't exist.
- **LangSmith tracing default** (flagged in the code review) — decide
  deliberately whether it's on in this deployment, given real user
  conversations will be flowing through once this is live.
