import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.api.router import api_router
from app.config import get_settings
from app.services.agent import create_agent
from app.services.checkpoint import setup_checkpoint
from app.services.llm import create_model

# Uvicorn only configures its own loggers, so app loggers (app.services.agent,
# app.api...) fell through to Python's lastResort handler: WARNING+ on stderr and
# nothing at INFO. Build diagnostics were therefore invisible in the terminal.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _n8n_mcp_connection(settings) -> dict:
    """Return the stdio connection config for the n8n-mcp server."""
    return {
        "n8n-mcp": {
            "transport": "stdio",
            "command": settings.n8n_mcp_command,
            "args": ["n8n-mcp"],
            "env": {
                "MCP_MODE": "stdio",
                "LOG_LEVEL": "error",
                "DISABLE_CONSOLE_OUTPUT": "true",
                "N8N_API_URL": settings.n8n_api_url or "http://localhost:5678",
                "N8N_API_KEY": settings.n8n_api_key or "",
            },
        }
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: settings, checkpointer pool, MCP tools, model, and agent graph."""
    settings = get_settings()
    app.state.settings = settings

    ckpt = await setup_checkpoint()
    app.state.checkpointer = ckpt.checkpointer
    app.state.ckpt_service = ckpt

    mcp_client = MultiServerMCPClient(_n8n_mcp_connection(settings))
    app.state.mcp_client = mcp_client
    try:
        mcp_tools = await mcp_client.get_tools()
    except Exception:
        # With the default `npx` command a cold boot reaches the npm registry, and a
        # registry hiccup here would otherwise take the whole app down. Strict mode
        # (the default) keeps that loud: the deploy fails instead of shipping an app
        # that reports healthy but can no longer build workflows. Operators who
        # would rather keep chat and history alive can set N8N_MCP_STRICT_BOOT=false.
        if settings.n8n_mcp_strict_boot:
            raise
        logger.exception(
            "n8n-mcp tool listing failed; starting with NO n8n tools — chat, planning "
            "and history keep working, but workflow builds cannot."
        )
        mcp_tools = []

    model = create_model()
    app.state.model = model

    agent = create_agent(
        model,
        mcp_tools,
        ckpt.checkpointer,
        validate_connection=_n8n_mcp_connection(settings)["n8n-mcp"],
    )
    app.state.agent = agent

    yield

    # MCP stdio sessions are spawned per tool call (self-hosted n8n-mcp); the
    # MultiServerMCPClient holds config only, so nothing to close here.
    app.state.mcp_client = None
    await ckpt.close()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application (settings, CORS, router)."""
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Return a simple liveness response."""
        return {"status": "ok"}

    return app


app = create_app()