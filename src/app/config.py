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
    n8n_api_url: str = ""
    n8n_api_key: str = ""

    cors_origins: Annotated[list[str], NoDecode] = []

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "botchain-ai"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()