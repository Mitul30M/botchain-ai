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
broken `DATABASE_URL` is the exact failure mode to avoid.

## Rollback / troubleshooting

- Deploy stuck? Check the job is awaiting approval — the `production` environment gate.
- Logs: Railway dashboard → service → deployments.
- Revert deploys: Railway "redeploy from previous". Disabling the repo var stops new deploys
  immediately without touching Railway.
- Stop-the-line: `gh variable set RAILWAY_DEPLOY_ENABLED --body false`.
