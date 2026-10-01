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

### WS9.4 — CI on pull requests
`.github/workflows/ci.yml`, three jobs on PRs: lint → test → image build.
- `uv sync --frozen --group dev`; `uv run ruff check src tests`; `uv run pytest`.
- **Test DB: `uv run pytest -m "not db"`.** Rationale: `tests/test_checkpoint.py` is the only
  real-DB test and it currently reads `DATABASE_URL`, which resolves to **production**
  (`ep-rapid-rain-…`, `br-aged-sunset-b39a2u2u`). Putting that URL in CI secrets would make
  **every PR write to production** — strictly worse than skipping it. Selecting it out is the
  only hermetic option until the Neon `tests` branch exists.
- Therefore WS9.4 **requires** declaring a `db` marker in `pyproject.toml` and marking
  `test_checkpoint.py` — a two-line change that is the smallest useful slice of Phase 8.
- `docker build` job on PR so image breakage is caught before merge.
- **Gate:** CI green on a throwaway PR with no secrets configured.

### WS9.5 — guarded deploy on `main`
Config only; the first real deploy stays in Phase 10.
- Trigger on `main`; require CI green + tag protection; environment-gated secrets.
- Prefer deploy **after** merge (post-merge `main`) over PR-triggered previews, to match
  Phase 10's "user runs it" scope.
- **Gate:** workflow file is valid; deploy job stays disabled/dry until Phase 10.

### WS9.6 — housekeeping
- Delete the stray root `main.py` shim (tracked; unused).
- Update `AGENTS.md` milestone table: mark 8 deferred, 9 done. Record the CI
  `pytest -m "not db"` decision and the deferred `tests` Neon branch.

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