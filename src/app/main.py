from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.services.checkpoint import setup_checkpoint


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: settings + checkpointer pool; shutdown: pool closes automatically."""
    app.state.settings = get_settings()

    ckpt = await setup_checkpoint()
    app.state.checkpointer = ckpt.checkpointer
    app.state.ckpt_service = ckpt

    app.state.agent = None      # Phase 5
    app.state.model = None      # Phase 5
    app.state.mcp_client = None  # Phase 5

    yield

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