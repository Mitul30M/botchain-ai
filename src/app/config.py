from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BotChain AI API"
    app_version: str = "0.1.0"

    database_url: str = ""

    kinde_issuer_url: str = ""
    kinde_audience: str | None = None

    ollama_api_key: str = ""
    mistral_api_key: str = ""
    mistral_capacity_retries: int = 3
    n8n_api_url: str = ""
    n8n_api_key: str = ""
    # How to launch the n8n-mcp stdio server. Local dev keeps the default `npx`,
    # which resolves the package on demand. The production image overrides this to
    # `n8n-mcp`, which it has pre-installed, so a cold boot never reaches the npm
    # registry (see _markdown/phase9/backend-phase9-plan.md, R2).
    n8n_mcp_command: str = "npx"
    # Strict (default): a failed n8n-mcp tool listing aborts startup, so a broken
    # MCP server is a failed deploy rather than an app that boots healthy but can
    # no longer build workflows. Set false to boot anyway with an empty tool list
    # (chat and history keep working; builds do not).
    n8n_mcp_strict_boot: bool = True

    cors_origins: Annotated[list[str], NoDecode] = []

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "botchain-ai"

    # Fallback USD-per-1M-token rates for models missing from MODEL_PRICING, so a
    # newly released model still records a real cost instead of pricing at zero.
    pricing_unknown_input_per_million: Decimal = Decimal("0.20")
    pricing_unknown_output_per_million: Decimal = Decimal("0.20")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()