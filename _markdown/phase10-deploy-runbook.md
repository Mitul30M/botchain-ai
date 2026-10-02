# Phase 10 — first Railway deploy: runbook

State after setup, before first deploy. Railway **project `graceful-creation`**, environment
`production`, service `botchain-ai`. Full narrative + evidence: `phase9/backend-phase9-plan.md`.

## Credentials

| What | Value |
|---|---|
| Project ID | `aaf080ac-78a0-4cde-9b7f-3a293af33317` |
| Service name (`RAILWAY_SERVICE`) | `botchain-ai` |
| Service ID (fallback) | `ab297608-65d5-4145-b13d-2e105138bf55` |
| Environment ID | `492d377a-5d5e-4a79-9c10-3369d341b771` |
| Token kind | Project-scoped, `production`-scoped → `RAILWAY_TOKEN` |
| Project name | `graceful-creation` (Railway-generated) |

## Already configured

- Repo vars `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE` — set.
- Repo var `RAILWAY_DEPLOY_ENABLED` — **deliberately unset** until step 3.
- GitHub `production` environment — created; hosts the `RAILWAY_TOKEN` secret.
- Autodeploy **disabled** in Railway (the load-bearing manual step: a linked service
  auto-deploys every push and would bypass CI entirely).

## The bug that cost an hour: `Authorization` vs `Project-Access-Token`

Project-scoped tokens must be sent as **`Project-Access-Token: <token>`**, not
`Authorization: <token>`. Two tokens were wrongly declared invalid because every probe used
the wrong header. Railway's CLI handles this internally (its binary contains the
lowercase `project-access-token` scheme and documents `RAILWAY_TOKEN` = project token), so
**`ci.yml` needs no change** — but any manual `curl` against the API must use the right header.

Verify a token without printing it:

```bash
curl -s https://backboard.railway.com/graphql/v2 \
  -H "Project-Access-Token: $RAILWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"query { projectToken { project { name } environment { name } } }"}'
```

Also note `me` is the wrong probe for a project token: `me` returns `User`, and a project
token has no user identity. Use `projectToken`.

## Steps

### 1. Set the secret (user, local shell)

```bash
printf '%s' "$RAILWAY_TOKEN" | gh secret set RAILWAY_TOKEN --env production
gh secret list --env production        # expect RAILWAY_TOKEN
```

`printf`, not `echo` — a trailing newline in a secret is painful to diagnose later.

### 2. Confirm Railway runtime variables (user, dashboard)

Set by hand, **sealed**, never by importing `.env.example`:

- `DATABASE_URL` — Neon **direct/unpooled** URL, not `-pooler`
- `MISTRAL_API_KEY`
- `KINDE_ISSUER_URL`
- `CORS_ORIGINS` — https://<frontend domain>
- `N8N_API_URL` / `N8N_API_KEY` — optional; core n8n-mcp tools work without an n8n instance

### 3. Arm the gate

```bash
gh variable set RAILWAY_DEPLOY_ENABLED --body true
```

### 4. Deploy

Push to `main`. CI runs `lint` + `test` + `docker` in parallel, then `Deploy (Railway)`
appears (no longer skipped) and waits for approval. Watch: `gh run watch`.

First build is slow: the image is ~1.02 GB with n8n-mcp pre-installed.

### 5. Domain + CORS

`railway domain` (or dashboard) to generate a public domain. Then set `CORS_ORIGINS` to
include it **and** the frontend origin, and redeploy — an unset `CORS_ORIGINS` means no CORS
middleware at all, which breaks the browser while `/health` stays green.

### 6. Smoke test

`/health` is shallow liveness and proves nothing about wiring. Use an authenticated
DB-backed route (`GET /api/v1/chats`) with a real Kinde token. A green `/health` with a
broken `DATABASE_URL` is the exact failure mode to avoid — and per Incident 2, a green
`/health` also does not prove the edge can reach the app.

Read the token silently and keep it on your machine. Pasting it into a ticket, chat or
transcript corrupts it and produces a 401 that looks like a config fault (see the
process note under Production incidents):

```bash
read -rs TOK; echo
curl -sS -o /tmp/c.json -w "HTTP %{http_code}\n" \
  -H "Authorization: Bearer $TOK" \
  https://<your-domain>/api/v1/chats
cat /tmp/c.json; unset TOK
```

Two different 401 bodies distinguish the failure classes: `Not authenticated` means no
token reached the app, while `Invalid or expired token` means one did and verification
failed.

## Production incidents

Both shipped green through CI and both presented to the outside world as the same
opaque `HTTP 502` from Railway's edge. They had nothing to do with each other, and
neither was visible from `/health`.

### Incident 1 — the wheel shipped without `app.prompts`

**Symptom.** Edge 502 on every path. Container logs showed
`ModuleNotFoundError: No module named 'app.prompts'` raised from `app/main.py` →
`app/services/agent.py` → `from app.prompts import (...)`. Uvicorn never started, so
the Dockerfile healthcheck failed and Railway restarted the container.

**Root cause.** `.gitignore` carried an unanchored `prompts/`, intended to exclude the
root prototype placeholder. In gitignore semantics an unanchored trailing-slash pattern
matches a directory of that name **at any depth**, so it also matched
`src/app/prompts/` — production code. Hatchling excludes VCS-ignored paths when
building, so `app/prompts/` never made it into the wheel, while `app/api`, `app/core`,
`app/models`, `app/schemas` and `app/services` all shipped.

**Why every earlier gate passed.**

| Gate | Why it missed it |
|---|---|
| `git ls-files src/app/prompts` | Clean — gitignore never affects **tracked** files |
| `pytest` (132 tests) | Imports from the `src` tree, where the files are present |
| `docker build` | Builds an image; imports nothing |
| Railway healthcheck | Container was crash-looping, correctly reported unhealthy |

The two mechanisms disagree — git tracks the files, the build backend ignores them — so
the repository looked completely healthy while the artifact was broken.

**Fix.** Anchor the prototype exclusions: `prototype/`, `notebooks/`, `/prompts`.

**Guard (now in CI).** New `package` job, and `deploy` gained it in `needs`:

1. asserts every subpackage under `src/app` is present in the built wheel — catches
   omissions the import graph never reaches;
2. installs the wheel into a clean venv and imports `app.main` with the repo root off
   `sys.path`, i.e. the same import path uvicorn takes in production.

Verified to fail on a deliberately reintroduced bug:
`subpackage(s) missing from wheel: app/prompts`.

**Lesson.** Unanchored directory patterns in `.gitignore` are load-bearing for packaging,
not just tidiness. Any new root-only ignore pattern should be written `/name`.

### Incident 2 — healthy container, unreachable container

**Symptom.** Edge 502 on `/health`, `/`, and `/openapi.json`, with
`x-railway-fallback: true`. Yet `docker inspect` showed the container healthy and the
Railway deployment reported success.

**Root cause.** Railway injects `PORT=8080`. `CMD` correctly ran
`uvicorn ... --port ${PORT:-8000}`, so uvicorn bound **8080**. The service domain had
been generated with target port **8000**, so the edge routed to a port nothing was
listening on.

**Why health passed.** The Dockerfile `HEALTHCHECK` also interpolates `${PORT:-8000}`,
so it probed 8080, got a 200, and reported healthy. The container genuinely was healthy —
just not reachable at the port the edge was using. Honoring `PORT` was correct; the
mismatch was in the Railway domain's target port.

**Fix.** Set the domain's target port to `8080`.

**Lesson.** A passing healthcheck proves the app binds, not that the edge can reach it.
After any deploy that generates or changes a domain, verify with a **public** request,
not by inspecting the container.

### Process note — three wrong diagnoses from one corrupted input

The 401s seen while chasing Incident 2 were **not** a bug. Kinde tokens pasted into a
transcript arrived with the payload mangled (`iss` variously
`https://mitul3-m.kinde.com`, `https://mitul3/m.kinde.com`) while the signature stayed
byte-identical. Such a token can never verify, so the app was correctly returning
`Invalid or expired token` — and the real config was right all along.

Two diagnoses were chased on that corrupted input before it was caught. The tell was
that an external decoder (jwt.io) read the same pasted string cleanly and returned the
issuer the server expected.

**Lesson.** When local verification of a token fails but an independent decoder shows
the token is well-formed and correct, suspect **input fidelity** before configuration.
For anything credential-shaped, keep it on the machine that owns it
(`read -rs TOK` + `curl`) and never paste it into a transcript — a token that cannot
survive the trip will manufacture convincing false failures.

## Rollback / troubleshooting

- Deploy stuck? Check the job is awaiting approval — the `production` environment gate.
- Logs: Railway dashboard → service → deployments.
- Revert deploys: Railway "redeploy from previous". Disabling the repo var stops new deploys
  immediately without touching Railway.
- Stop-the-line: `gh variable set RAILWAY_DEPLOY_ENABLED --body false`.
