# Backend plan — Phase 9: containerise + CI (botchain-ai)

## Read first
1. `_markdown/python-fastapi-backendchecklist.md` — Phase 9 row of the milestone table.
2. `AGENTS.md` — settled decisions (Railway single replica, Prisma-owns-schema, uv-only).
3. `_markdown/phase7/backend-phase7-plan.md` — house style for these docs.
4. `_markdown/phase8/` — Phase 8 is **deliberately deferred**; see "Carried from Phase 8" below.
5. This document.

## What the checklist demands
> Multi-stage `Dockerfile` (uv build → slim runtime, node for `npx n8n-mcp`);
> `.github/workflows/ci.yml` (ruff + pytest + docker build on PR; guarded deploy on main).
> **Verification gate:** `docker build` passes locally.

Phase 10 ("First deploy") is explicitly out of scope — the user runs it.

## Current state (verified, not assumed)
Greenfield: **no `Dockerfile`, no `.dockerignore`, no `.github/`** exist yet.

| Fact | Verified how | Consequence |
|---|---|---|
| `requires-python = ">=3.14.6"`, `.python-version` `3.14.6` | `pyproject.toml`, `.python-version` | base must be `python:3.14-slim` (or newer) |
| build backend `hatchling`, `packages = ["src/app"]` | `pyproject.toml:37-42` | wheel is self-contained; no `src/` on `PYTHONPATH` needed |
| **zero runtime file reads in `src/app`** | grep for `Path(__file__)`/`read_text()`/`importlib.resources`/`files(` → 0 hits | prompts are Python modules (`src/app/prompts/*.py`), so **no prompt-file `COPY`** and no "missing prompts in slim image" failure mode |
| `main.py:56` calls `mcp_client.get_tools()` inside `lifespan` | `src/app/main.py` | **`npx` is a hard boot dependency** — app cannot start without it |
| `main.py:31` invokes bare `npx n8n-mcp` (no `--yes`, no pinned binary) | `src/app/main.py:26-42` | fresh container has no `~/.npm/_npx` cache → registry fetch per cold start |
| `n8n-mcp` **2.90.0**, `engines.node >=20.0.0` | `npm view n8n-mcp` | any Node ≥ 20 works; local dev is on v25.9.0 (odd = non-LTS) |
| `config.py:10` sets `env_file=".env"` | `src/app/config.py` | real env vars win over `.env` → inject at runtime, **never `COPY .env`** |
| `alembic/env.py:40` sources the URL from settings, **ignores** the dummy `sqlalchemy.url` | `alembic/env.py`, `alembic.ini:89` | prod is already stamped → **do not ship alembic in the runtime image** |
| **no `.dockerignore`**, `.venv` = 258M, `store/` = 2M, `.env` present | `ls`, `du` | `.gitignore` does **not** apply to Docker context → secrets leave the machine on every build |
| Docker daemon available locally | `docker version` → server **29.4.0** | checklist gates can be verified on this machine (was blocked at draft time) |
| stray tracked `main.py` at repo root: `print("Hello from botchain-ai!")` | `cat main.py`, tracked in git | uv-init leftover; nothing imports it; would land in the image |

## Blocking risks to retire before the gate

### 🔴 R1 — `.dockerignore` is a security control, not hygiene
Without it, `docker build` transmits ~264 MB — **including `.env` with `MISTRAL_API_KEY`,
the Neon URL, and the Kinde issuer** — to the daemon. Treat WS9.1 as non-optional regardless
of what else slips. Must exclude: `.env*`, `.venv`, `.git`, `store/`, `sandbox/`,
`prototype/`, `notebooks/`, `prompts/`, `_markdown/`, `_nextjs_repo_context/`, caches.

### 🔴 R2 — n8n-mcp is fetched from npm on every cold boot
`docker build` never exercises the MCP path, so **the image will build cleanly and then
fail or hang on Railway healthcheck**. Mitigations, in preference order:
- (a) `npm i -g n8n-mcp` in the builder layer; point `main.py`'s `command` at the resolved binary;
- (b) keep `npx n8n-mcp` but pre-warm an npm cache layer;
- (c) do nothing — accept a registry fetch per cold start. **(not recommended)**

Note (a) touches `main.py` in *both* places the command is built: `_n8n_mcp_connection()`
and the `validate_connection=` argument at `main.py:65`. They must stay consistent.

### 🟡 R3 — native wheels on Python 3.14
`psycopg[binary]` avoids libpq compilation, and the rest of the graph stack is pure Python,
so a compiler-free slim builder *should* work. Not verified. First `uv sync` inside the
image is the check; if anything needs building, add a throwaway build stage rather than
promoting a toolchain into the runtime.

### 🟡 R4 — local gate: RESOLVED
OrbStack was not running when this plan was drafted (`docker version` →
*dial unix … no such file*). OrbStack is now started — daemon **29.4.0**, and WS9.1's gate
has been satisfied against it. WS9.2 can be verified locally.

## Workstreams

### WS9.1 — `.dockerignore` — ✅ **DONE** *(first; independent; unblocked everything else)*
Allowlist-style (explicit includes) over denylist — new secret files then fail *closed*.
- Tasks: write allowlist for `pyproject.toml`, `uv.lock`, `src/`, `README.md`; keep
  `alembic*` out (see WS9.2); never include `.env*`, `.venv`, `.git`, `store/`, `sandbox/`.
- Files: `.dockerignore`
- **Gate: MET.** Verified with a real `docker build` (throwaway Dockerfile kept outside the
  repo via `-f`, context = repo root, so nothing extra was committed):
  - context top level is **exactly** `README.md`, `pyproject.toml`, `src/`, `uv.lock`
  - **48 files, 500 KB** — down from ~264 MB
  - absent: `.env`, `.venv`, `.git`, `store/`, `.python-version`
  - present: all four build inputs, incl. `src/app/prompts/plan.py` (the root-anchored
    `/prompts` deny correctly does **not** touch `src/app/prompts/`)
  - no `__pycache__` under `src/`

#### 🔴 Gotcha found while verifying this — worth remembering
**Docker's `.dockerignore` has no inline-comment syntax.** A `#` only starts a comment as
the *first* character of a line; trailing text becomes part of the pattern. The first
draft of this file annotated patterns inline:

```
!pyproject.toml          # deps, build backend, [tool.hatch.build] packages
```

Docker parsed that entire line as one pattern, it matched nothing, **every re-include
silently failed, and the build context shipped 0 files.** It failed *closed* (no secret
leaked) but the build was broken. The same bug turned the "belt-and-braces" deny lines
into no-ops — harmless here only because `*` had already excluded those paths.

All comments in `.dockerignore` are now on their own lines, with an in-file warning not to
"tidy" them back into trailing form. **This trap is silent: it never warns, it just omits
what you asked for.** Anyone adding patterns to that file must keep comments standalone.

### WS9.2 — multi-stage `Dockerfile` — ✅ **DONE**
Shape: `builder` (tooling, compilers, Node) → `runtime` (venv + Node only).
- **Builder:** `python:3.14.6-slim`, `uv==0.11.25`, `uv sync --frozen --no-dev
  --no-editable`, plus Node 24.21.0 LTS + `n8n-mcp@2.90.0` (pinned).
- **Runtime:** same Python base, no uv, no compiler; the `.venv` and `/opt/node` are
  copied wholesale, so Node is never re-downloaded on later builds.
- Non-root `botchain` user; `apt` lists purged; `/opt/node/include` (65 MB of C headers,
  dead weight once n8n-mcp is pre-compiled) removed.
- Base pinned to exactly **3.14.6** to match `.python-version`; `UV_PYTHON_DOWNLOADS=never`
  keeps uv off the network. `TARGETARCH` is mapped so one Dockerfile builds both arm64
  (local) and amd64 (Railway).
- Does **not** copy `alembic/`, `alembic.ini`, `tests/`, or `prototype/`.
- `CMD` runs `exec uvicorn … --host 0.0.0.0 --port ${PORT:-8000}` (no `--reload`), so
  uvicorn is PID 1 and receives SIGTERM directly. `HEALTHCHECK` hits `/health` using
  `$PORT` at runtime.
- **Gate: MET.** `docker build` succeeds; verified in the built image:
  - `uv`, `gcc`, `cc`, `make` → all **ABSENT**; running as **botchain** (non-root)
  - `node v24.21.0`, `n8n-mcp` at `/opt/node/bin/n8n-mcp`, `npx` present
  - `import app.main` → **OK** (so the `--no-editable` wheel install works without `src/`)
  - `/build/.venv/alembic.ini` → **absent**, i.e. the container has no way to run a
    migration. (The alembic *library* does ship — it is a declared dependency — but is
    inert without the config. The earlier version of this doc implied otherwise.)
  - `n8n-mcp` **starts** in the image and exits cleanly on `STDIN_CLOSE` → R2 confirmed
    functional, not merely present.

#### Image size baseline — **1.02 GB** (was 1.10 GB before the `include/` trim)
| Path | Size |
|---|---|
| `/build/.venv` | 333 MB |
| `/opt/node` | 272 MB (was 337 MB) |
| `/usr/local/lib/python3.14` | 30 MB |

Not yet addressed — recorded for a later dependency pass, **not** a Dockerfile problem:
`jedi` (31 MB, a code-completion lib), `zstandard` (21 MB), and the transitive provider
SDKs (`anthropic`, `google`, `tokenizers`, `hf_xet`, ~48 MB combined) pulled in by
langchain. `ipywidgets` and `rich` are also in `dependencies` and look like
prototype-era leftovers. Trimming means editing `pyproject.toml` + `uv.lock`, so it
needs its own decision — it is **not** required for a working deploy.

### WS9.3 — runtime contract
- **PORT binding:** Railway injects `$PORT` and requires `0.0.0.0`. Current dev command
  (`uvicorn app.main:app --reload`) does not do this. CMD must be
  `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`.
- **`--reload` must not ship.** It is a dev-only behaviour that forks a watcher.
- **Apply R2(a)**: pre-installed n8n-mcp binary, or explicitly accept (b)/(c).
- **Lifespan pre-flight (decide, recommend yes):** `get_tools()` at boot means a registry
  failure takes the whole app down. Consider warming the tool list at build time or
  tolerating an empty MCP tool set at boot. Behaviour change — needs sign-off.
- **Gate:** `docker run` boots, `GET /health` → 200 **with no npm cache present**, and the
  agent graph is built with a real tool list.

### WS9.3 — runtime contract — ✅ **DONE**
- **PORT binding + `--reload`:** `CMD` runs `exec uvicorn app.main:app --host 0.0.0.0
  --port ${PORT:-8000}`. `exec` makes uvicorn PID 1 so SIGTERM is delivered directly
  instead of being swallowed by a shell; `--reload` is deliberately absent (dev-only,
  and it would fork a watcher in the container).
- **R2(a) `npx` → pre-installed binary:** `main.py` no longer hardcodes `"npx"`. It reads
  the new `Settings.n8n_mcp_command` (**default `npx`**, so local dev is unchanged) and
  the image sets `N8N_MCP_COMMAND=n8n-mcp`. Same pattern as the `services/llm.py`
  "swap one file" rule. **Not a single hardcoded string** — both the `get_tools()` call
  and the `validate_connection=` argument go through `_n8n_mcp_connection()`, so they
  cannot drift apart (pinned by a test).
- **Lifespan pre-flight — decided to stay STRICT by default.** A failed tool listing
  aborts startup, so a broken n8n-mcp is a *failed deploy* rather than an app that
  reports healthy but can no longer build workflows. Opt-in degradation exists via
  `N8N_MCP_STRICT_BOOT=false`, which boots with an empty tool list and logs loudly.
  Rationale: R2(a) already removes the registry fetch, so the failure being defended
  against is far less likely — silently degrading would only trade a visible outage for
  an invisible one.
- **Gate: MET.** Image rebuilt and actually booted:
  - container reached **`healthy`**; `GET /health` → `200 OK`; `Application startup complete`
  - `setup_checkpoint()` completed against the real DB, proving DB wiring end-to-end
  - `N8N_MCP_COMMAND=n8n-mcp` confirmed baked into the image
  - `n8n-mcp` spawned and shut down cleanly (`STDIN_CLOSE`) → **no npm registry fetch**
  - local dev unaffected: `n8n_mcp_command == "npx"`, `strict_boot is True`
  - **11 new tests** in `tests/test_mcp_command.py` (135 total, ruff clean)

#### ⚠️ Found while booting: the n8n-mcp child env is a hardcoded allowlist
Booting the image printed n8n-mcp's **telemetry banner**, i.e. the third-party package
phones home on every cold start. Setting container-level `DISABLE_TELEMETRY=true` did
**nothing** — not a wrong variable name, but because `_n8n_mcp_connection()` passes an
explicit `env` dict to the stdio child, so the child sees only those 5 keys and inherits
nothing from the parent process.

**Fix is one line** — add `"DISABLE_TELEMETRY": "true"` to that dict in `main.py`.
**NOT applied**, because whether to opt out of a dependency's anonymous usage stats is a
product/policy decision for the repo owner, not a containerisation detail. Flagged rather
than decided. Note the same allowlist means *any* future MCP env var must be added there
too, or it will silently not arrive.

### WS9.4 — CI on pull requests — ✅ **DONE** *(verified green on real GitHub runners)*
`.github/workflows/ci.yml`, three independent jobs (parallel, not sequential, for faster
PR feedback) triggered on `pull_request` **and** pushes to `main`, so the branch WS9.5
deploys from is always verified. `concurrency` cancels superseded runs.

| Job | Does | Notes |
|---|---|---|
| `lint` | `ruff check src tests` | — |
| `test` | `pytest -m "not db" -v` | **secret-free** — see below |
| `docker` | `docker/build-push-action` build | `platforms: linux/amd64`, gha layer cache |

- **Action versions pinned, and the pin was earned the hard way.** `actions/checkout` **v7**,
  `docker/build-push-action` **v7**, `docker/setup-buildx-action` **v4**,
  `astral-sh/setup-uv` **v10.2.0**.
  The first real run failed with `Unable to resolve action astral-sh/setup-uv@v10,
  unable to find version v10` — setup-uv's **v10 line publishes no floating `@v10` tag**,
  so a floating major that looks current is unresolvable. **Pinned to exact `v10.2.0`.**
  `actionlint` cannot catch this: it does not resolve action refs over the network, so a
  syntactically valid, nonexistent ref passes lint and only fails on a real runner. Every
  non-comment action ref in `ci.yml` was subsequently verified to return HTTP 200 against
  the GitHub API, and that check is recorded as a comment in the workflow.
- **The amd64 pin is deliberate.** Local builds only exercise arm64, so CI is the only
  place the Railway target architecture gets built.
- **Test DB: `pytest -m "not db"`.** `tests/test_checkpoint.py` is the only suite touching a
  real database and it reads `DATABASE_URL`, which resolves to **production**. Putting that
  in CI secrets would make **every PR write to production** — strictly worse than skipping.
  Declared a `db` marker in `pyproject.toml` and applied it to that file (the smallest
  useful slice of Phase 8). Verified the split is exactly 3 / 132 with **zero**
  unknown-marker warnings.
- **Gate: MET.** `actionlint` clean (exit 0) locally, and the first real push proved it end
  to end on GitHub-hosted runners. Run **`36969637517`** on `main` (after the setup-uv pin,
  commit `811ffc5`) is **fully green**:

  | Job | Conclusion |
  |---|---|
  | `Lint (ruff)` | ✅ success |
  | `Test (pytest, no database)` | ✅ success |
  | `Image (docker build)` | ✅ success — `linux/amd64`, the Railway target, built on a real runner |
  | `Deploy (Railway)` | ⏭️ skipped — correct, `RAILWAY_DEPLOY_ENABLED` is unset |

  The preceding run **`36969318018`** is the useful negative result: `docker` passed while
  `lint`/`test` failed on the bad ref, and the deploy job skipped rather than firing on a
  partially-green pipeline. **The gate behaves as designed.**

### WS9.5 — guarded deploy on `main` — ✅ **DONE** *(inert until Phase 10)*
Config only; the first real deploy stays in Phase 10.

**Deploy job lives in `ci.yml`, not a separate `deploy.yml`.** This deviates from the
original sketch on purpose: `needs` can only reference jobs in the *same* workflow, so a
separate file could not have gated on CI being green — which is the entire point of a
"guarded" deploy. One file, one gate, and the job is skipped on PRs so it costs nothing.

- **Mechanism: CI-driven `railway up`**, chosen over Railway's GitHub integration because
  that integration deploys on *any* push to the linked branch, red CI included. Here
  `needs: [lint, test, docker]` means a red lint, test, or image build blocks the deploy.
- **Inert by default.** `if: github.ref == 'refs/heads/main' && vars.RAILWAY_DEPLOY_ENABLED == 'true'`.
  Phase 10 flips one repository variable. Chosen over a commented-out block precisely
  because a live-but-skipped job still gets actionlint-validated; commented YAML does not.
- **`environment: production`** — put required reviewers on it in repo settings for the
  human-approval gate, and scope `RAILWAY_TOKEN` there so it never reaches PR runs.
- **Corrected before writing it:** `railwayapp/railway-action` **does not exist** (404).
  Railway's documented path is the CLI, so the job installs it via
  `bash <(curl -fsSL https://railway.com/install.sh) -y` and runs
  `railway up --ci --project … --environment production --service …`.
  `--ci` exits when the *Railway* build completes, so a failed remote build fails the job
  rather than reporting a green deploy. `--project` requires `--environment`.
- **CLI on the runner, not `container: ghcr.io/railwayapp/cli:latest`** — that image is
  Alpine-based (no `bash`, so `run:` steps fail) and `railway up` needs the checked-out
  source tree in the workspace to upload. Trade-off accepted: a remote install script runs
  in CI. If that ever matters, pin a `railwayapp/cli` release binary instead.
- **`railway.json`** pins `builder: DOCKERFILE`, so Railway cannot silently fall back to
  Railpack/Nixpacks and fail to detect the build plan. Validated against Railway's live
  `railway.schema.json`, where `DOCKERFILE` is a documented `const`.
- **Gate: MET.** `actionlint` exits 0 on the full workflow including the deploy job.

**Four landmines recorded for Phase 10** (found while reading the code, not yet fixed):
1. **`CORS_ORIGINS` unset ⇒ no CORS middleware at all.** `main.py` only adds it
   `if settings.cors_origins`, so an unset value silently breaks the browser with a green
   `/health`. Must be set on Railway.
2. **Do not accept Railway's "import variables from `.env`" suggestion.** The committed
   `.env.example` holds placeholders (`user:password@host`, blank `MISTRAL_API_KEY`);
   importing them deploys an app that boots and then fails.
3. **`/health` is shallow liveness** — it never touches the DB or MCP. A green Railway
   healthcheck proves nothing; verify against a real DB-backed route after deploying.
4. **`railway up` never creates a public domain** — run `railway domain` separately.

Boot contract confirmed from `config.py` / `main.py`: `DATABASE_URL` and `MISTRAL_API_KEY`
are hard requirements (empty ⇒ engine/Mistral raises during startup); `KINDE_ISSUER_URL`
silently degrades to 401 on every authenticated route; `N8N_API_URL`/`N8N_API_KEY` fall back
to `localhost:5678` (core MCP tools work without them). Neon `production` was re-verified
as `ready` (it reads `archived` in older notes) on `ap-southeast-1`, and the backend
`DATABASE_URL` is correctly the direct, unpooled host. The image ships no
`alembic.ini`/`versions/` and the baseline `3169311c48d2` is already stamped, so **no
migration step is needed at deploy time**.

### WS9.6 — housekeeping — ✅ **DONE**
- Deleted the stray root `main.py` shim (`uv init` leftover, `print("Hello from
  botchain-ai!")`). Verified unused before deleting: zero references anywhere, and
  `pyproject.toml`'s hatch config packages only `["src/app"]`, so it was never in the
  wheel. It was already excluded from the Docker context by the `.dockerignore` allowlist.
- Corrected `AGENTS.md`: the milestone table claimed Phase 7, 8 **and** 9 were "Not
  started". Phase 7 was already shipped and merely mislabelled (`TemporaryDirectory` at
  `services/agent.py:736`, download route at `api/v1/messages.py:545`) — fixed, not
  overwritten. Phase 8 marked deferred-by-choice, Phase 9 done, Phase 10 deferred with a
  pointer to the WS9.5 prep notes.
- Recorded the two decisions Phase 6.6/WS9.4 made: CI runs `pytest -m "not db"` so it
  stays secret-free, and the `tests` Neon branch is still uncreated (resume criterion
  stated). Also corrected the "Running / developing" section, which claimed `uv run
  pytest` uses the `tests` Neon branch — it does not, it resolves to production.

**Gate: MET.** 135 pytest green (132 non-DB), ruff clean, `actionlint` exit 0, no dangling
references to the deleted shim.

## Carried from Phase 8 (deliberately deferred)
Full deferral agreed: no `conftest.py`, no Neon `tests` branch, no route-smoke or
graph-transition work. Phase 8 was re-scoped to one 10-minute safety slice
(`TEST_DATABASE_URL` guard in `test_checkpoint.py`) — **optional, not required**, because the
write is confined to LangGraph's scratch tables under the literal thread_id
`"test-round-trip"`, which no real chat UUID can collide with. Untidiness, not a risk.
**Trigger for revisiting: when you next change code** (regression risk), or in Phase 9 if
you choose a real CI test DB.

## Decisions needed
1. **R2(a) npx → pre-installed binary?** Recommended. Costs a `main.py` edit in two places.
2. **Ship alembic in the runtime image?** Recommended **no** — prod is already stamped, and
   your rules forbid the app ever migrating the Prisma-owned schema.
3. **Node version pin:** recommend `node:24-slim` (LTS) over the local v25 (odd/non-LTS).
   `n8n-mcp` needs only ≥20.
4. **Lifespan pre-flight tolerance** (WS9.3) — yes/no; behaviour change.
5. **Confirm `pytest -m "not db"`** so the Neon `tests` branch stays deferred.

## Verification
- `docker build -t botchain-ai:phase9 .` succeeds — **requires OrbStack running**.
- `docker run` with injected env only → `GET /health` returns 200, and `/api/v1/chats`
  answers (proves DB wiring without a build).
- Boot with a cleared npm cache to prove R2 is actually retired.
- `docker images` size recorded in this doc as the baseline.
- CI green on a throwaway PR, with **no** secrets configured.

---

# Phase 10 — first Railway deploy — ✅ **DONE**

WS9.5's guard is built and proven; this phase executes it. **Architecture A (CI-driven
`railway up`) is retained deliberately** — see "Why not Railway's Wait for CI" below.

## Completed

- **Default branch renamed `master` → `main`.** CI and the WS9.5 deploy gate both key on
  `main`, so this was a prerequisite. Pushed `origin/main`, GitHub default updated.
  `origin/master` still exists and is now unused — safe to delete, left alone deliberately.
- **First real CI run** `36969318018` — `docker` ✅ / `lint` ❌ `test` ❌ on the `setup-uv@v10`
  ref. Deploy job correctly **skipped** rather than firing on a half-green pipeline.
- **setup-uv pinned** to `v10.2.0` (commit `811ffc5`); re-run **`36969637517` fully green**.
  WS9.4's gate is now genuinely met — see its section above.
- **Railway project + service created**, GitHub repo connected as the source.
- **GitHub `production` environment created** (`gh api -X PUT .../environments/production`).
  This is what scopes `RAILWAY_TOKEN`, so the deploy token is *not* readable from PRs or from
  anyone without access to that environment.
- **Repository variables set:** `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE`.
  `RAILWAY_DEPLOY_ENABLED` is deliberately **still unset** — the deploy stays inert until a
  working token exists, so a stray push cannot deploy.
- **Autodeploy DISABLED** in Railway service settings. This is the single most important
  manual step: a GitHub-linked service auto-deploys on every push to the linked branch and
  would deploy **regardless of CI**, completely bypassing the gate. With it off, the only
  path to production is the `needs: [lint, test, package, docker]` job.
- **Deploy executed.** Run `36977212110` went green end-to-end after two fixes — the Railway
  CLI is not on `PATH` in a non-login `run:` step (needs `$GITHUB_PATH`), and the
  `RAILWAY_TOKEN` secret had to be set on the **`production` environment** rather than the
  repo. An empty-token guard now fails loudly instead of surfacing a generic auth error.
- **Two production incidents on first boot**, both presenting as an edge `HTTP 502` and both
  invisible to `/health`. Full write-ups, including why every gate passed them, are in
  `_markdown/phase10-deploy-runbook.md` → *Production incidents*:
  1. an unanchored `prompts/` in `.gitignore` made hatchling drop `app/prompts/` from the
     wheel, so the container crash-looped with `ModuleNotFoundError` **after** a green build;
  2. Railway injects `PORT=8080` while the domain's target port was `8000`, so the app was
     healthy and genuinely reachable — just not at the port the edge routed to.
- **CI guard added** for incident 1, and `deploy` now depends on it. The `package` job asserts
  every `src/app` subpackage ships in the wheel **and** imports `app.main` from the installed
  wheel with the source tree off `sys.path`. Proven to fail on a reintroduced bug
  (`subpackage(s) missing from wheel: app/prompts`); `actionlint` clean.
- **`verify_token` no longer swallows its failure reason** (`src/app/core/security.py`). It
  caught `jwt.JWTError` and re-raised a generic 401, which made an issuer/audience mismatch
  indistinguishable from a bad signature. It now logs the jose error, `alg`, `kid`, the
  configured issuer beside the token's own `iss`, and the configured audience — never the
  token. Same 401, same headers; behaviour unchanged.
- **Verified live.** `https://botchain-ai-production.up.railway.app/health` → `200`, and
  `GET /api/v1/chats` with a real Kinde token → `200` returning production Neon rows. All
  11 v1 routes are registered and served.

## Blocker — RESOLVED (no valid Railway API token)

Two tokens were issued and **both were rejected** by Railway's GraphQL API
(`{"errors":[{"message":"Not Authorized"}]}`, HTTP 200) and by the official CLI v5.63.1
(`railway whoami` → `Unauthorized`). Both were well-formed 36-char UUIDs, and the API was
confirmed reachable, so this is **account-level, not a typo**. Working hypotheses:

1. **Unverified account email** — most likely. Railway does not activate API tokens until the
   account email is verified, which fails every token identically.
2. The token *name/ID* was copied instead of the secret value.

Neither token was ever wired into CI, so nothing is half-configured.

**Standing rule adopted for this phase: tokens are never pasted into chat.** A token is
exported into the user's own shell and verified with a `curl` from there; only
"valid / not valid" comes back to me. Tokens pasted so far are treated as compromised
and must be revoked.

**Resolution.** Neither hypothesis above was it. A Railway **project** token is
authenticated with the `Project-Access-Token` header, not `Authorization`, and the query
selects via `projectToken` — `me` is invalid for a project token. Against `Authorization`
a perfectly valid token returns `{"errors":[{"message":"Not Authorized"}]}` with HTTP 200,
which is exactly the misleading signal that made this look like an account-level problem.
The CLI (`railway v5.63.1`) accepts the same token once passed correctly.

**This rule was subsequently violated, and it cost real time.** A Kinde JWT was pasted
into the conversation to debug a 401. It arrived corrupted — the payload was mangled while
the signature stayed byte-identical, so it could never verify. That produced convincing
false failures and two wrong diagnoses before an independent decoder (jwt.io) read the
same pasted string cleanly. See the process note in the Phase 10 runbook. The rule was
right; the fix is `read -rs TOK` on the machine that owns the credential.


## Why not Railway's native "Wait for CI"

Railway can gate auto-deploys on GitHub Actions itself, and our workflow already satisfies
its requirement (`on: push: branches: [main]`). It was rejected because:

- It gates on **workflow-run conclusions**, not our **four named jobs** — strictly weaker
  than `needs: [lint, test, package, docker]`.
- The Railway docs explicitly warn against pairing it with a concurrency group that cancels
  queued runs. Ours sets `cancel-in-progress: true`, so a superseded run is **cancelled**,
  and a cancelled workflow can block a deployment.
- It gives up the `environment: production` human-approval gate and needs **no** Railway
  token in GitHub at all.

Keeping WS9.5 as built costs one scoped secret; the wait-for-CI footgun costs correctness.

## Remaining

Done during Phase 10: the project token, **replicas = 1** + Southeast Asia, sealed
`DATABASE_URL` / `MISTRAL_API_KEY` / `KINDE_ISSUER_URL`, `RAILWAY_DEPLOY_ENABLED=true`,
the public domain, and the authenticated DB-backed smoke test (`/api/v1/chats` → `200`).

Still open:

1. **`CORS_ORIGINS` is unset** — the last thing blocking frontend use. Unset means *no* CORS
   middleware is added at all, so a browser cannot call the API even though every route is
   healthy. Set it to the frontend origin (plus the backend origin if the UI needs it), then
   redeploy.
2. **Rotate the Railway project token.** It has deploy rights on `graceful-creation` and was
   pasted into a conversation; regenerate, update the `production` environment secret, revoke
   the old one. The Kinde JWT pasted while debugging was a live session token — revoke by
   logging out of the Kinde app.
3. **n8n-mcp telemetry is live in production** and printing an installation ID. Decision is
   the owner's; opting out needs `"DISABLE_TELEMETRY": "true"` in the *explicit* child env in
   `src/app/main.py`, because the image-level variable cannot reach it while the child
   environment is set wholesale.
4. **Required reviewer on the `production` environment** — UI only; the REST API rejects
   User-type reviewers (`422 App not installed on organization`). Self-approval is allowed by
   default ("prevent self-reviews" is opt-in), so a solo owner can gate their own deploy.
5. **`railway.json` is deprecated.** Config as Code is being replaced by `.railway/railway.ts`;
   the existing file keeps working until **2026-12-01**, so this is on the clock, not urgent.
6. **Open follow-up from the first-boot logs:** `n8n-mcp` reported `stdin closed, shutting
   down...`. Startup completed and the app is healthy, so it was not fatal, but if n8n tool
   calls fail during a real build, that is the first thing to look at.
