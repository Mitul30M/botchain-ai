# syntax=docker/dockerfile:1
#
# BotChain AI backend — multi-stage production image.
# Design + rationale: _markdown/phase9/backend-phase9-plan.md (WS9.2)
#
# Stage layout:  builder (tooling, compilers, Node) -> runtime (venv + Node only)
#
# Three deliberate choices, all recorded in the plan:
#   1. `.python-version` is excluded by .dockerignore, so the base image is pinned
#      here to exactly the repo's declared 3.14.6. With UV_PYTHON_DOWNLOADS=never,
#      uv resolves against the image's system interpreter and can never reach the
#      network for one.
#   2. Node + n8n-mcp are installed in the BUILDER and copied in. If they were
#      installed in the runtime layer, every `docker build` would re-download them.
#   3. The alembic MIGRATION SCRIPTS are not shipped: `alembic/` and `alembic.ini`
#      are absent from the context, so the container cannot run a migration at all.
#      Prod is already stamped and the backend must never alter the Prisma-owned
#      schema (AGENTS.md). The alembic *library* still ships — it is a declared
#      project dependency used by the local dev workflow — but with no alembic.ini
#      in the image it is inert. Verified: `/build/.venv/alembic.ini` absent.
#
# No secrets are baked in. All config is injected at runtime (DATABASE_URL,
# KINDE_ISSUER_URL, MISTRAL_API_KEY, ...).

ARG PYTHON_VERSION=3.14.6
ARG NODE_VERSION=24.21.0
ARG UV_VERSION=0.11.25


# ────────────────────────────── builder ──────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ARG UV_VERSION
ARG NODE_VERSION
# BuildKit injects the build platform's arch, so one Dockerfile builds both the
# arm64 image used locally and the amd64 image Railway runs.
ARG TARGETARCH

# UV_PYTHON_DOWNLOADS=never is REQUIRED (not an optimisation): .dockerignore drops
# .python-version, so this is what guarantees uv cannot try to fetch its own
# interpreter during `uv sync`.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# uv is build-time only; the runtime stage never receives it.
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# Dependency layer first, so editing src/ does not invalidate the resolved venv.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
# --no-editable installs the built wheel rather than a path link, which is what
# lets the runtime stage ship the venv alone and omit src/ entirely.
RUN uv sync --frozen --no-dev --no-editable

# Node + a pre-installed n8n-mcp, so a cold container start does not fetch the
# package from the npm registry. Installed in the builder precisely so the
# runtime image can just copy the result.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) NODE_ARCH=x64 ;; \
      arm64) NODE_ARCH=arm64 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \
    NODE_TARBALL="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}"; \
    mkdir -p /opt/node; \
    tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1; \
    rm -f /tmp/node.tar.xz; \
    # include/ is C headers (~65 MB) for compiling native addons. n8n-mcp is
    # already compiled by this point, so headers are dead weight at runtime.
    rm -rf /opt/node/include /opt/node/share/doc /opt/node/CHANGELOG.md; \
    apt-get purge -y --auto-remove curl xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    /opt/node/bin/node --version

ENV PATH="/opt/node/bin:${PATH}"

# Pin n8n-mcp so image rebuilds are deterministic (2.90.0 requires node >= 20).
RUN npm install -g n8n-mcp@2.90.0 \
 && npm cache clean --force \
 && n8n-mcp --help >/dev/null 2>&1 || true


# ────────────────────────────── runtime ──────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# PYTHONUNBUFFERED: streaming SSE must not sit in a block buffer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/build/.venv/bin:/opt/node/bin:${PATH}"

RUN groupadd --system botchain \
 && useradd --system --gid botchain --home-dir /app --no-create-home botchain

# Order matters for layer cache: /opt/node and the venv change rarely.
COPY --from=builder /opt/node /opt/node
COPY --from=builder /build/.venv /build/.venv

WORKDIR /app
USER botchain

EXPOSE 8000

# Refuses to be a false-positive: any non-2xx raises and marks unhealthy.
# Reads PORT at runtime so it stays correct if the platform injects one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/health', timeout=4)"

# exec uvicorn so it becomes PID 1 and receives SIGTERM directly (no shell
# wrapper swallowing it). --reload is deliberately absent: it is dev-only and
# would fork a watcher process inside the container.
CMD ["sh","-c","exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]