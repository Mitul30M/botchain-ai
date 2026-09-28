# from langchain_ollama import ChatOllama
from langchain_mistralai import ChatMistralAI

from app.config import get_settings

# --- Ollama (commented out 2026-09-28 while evaluating Mistral; swap back by
#     uncommenting the import + create_model body below and restoring the
#     Ollama return) ---
# _OLLAMA_MODEL = "gemma4:cloud"
# _OLLAMA_BASE_URL = "https://ollama.com"

# Mistral — candidate names to try for this project (verified 2026-09-28 on the
# free API tier: mistral-small-latest -> 429 (0 req/min), mistral-large-latest
# -> 403 "not available", so use the open models below — both 200, ~188 req/min):
#   ministral-8b-latest, open-mistral-nemo (open-mistral-7b)
_MISTRAL_MODEL = "ministral-8b-latest"

# Public name of the model in use — stored on each Chat.model at creation so the
# UI shows the real model instead of the DB's "claude-sonnet-4-6" placeholder.
MODEL_NAME = _MISTRAL_MODEL


def create_model() -> ChatMistralAI:
    """Create the application's chat model (Mistral, temperature-tuned).

    The single provider touchpoint — swapping backends touches only here.
    """
    settings = get_settings()
    return ChatMistralAI(
        model=_MISTRAL_MODEL,
        api_key=settings.mistral_api_key or None,
        temperature=0.2,
    )
    # Ollama cloud version (kept for reference while Mistral is active):
    # return ChatOllama(
    #     model=_OLLAMA_MODEL,
    #     base_url=_OLLAMA_BASE_URL,
    #     client_kwargs={
    #         "headers": {"Authorization": f"Bearer {settings.ollama_api_key}"}
    #     },
    #     temperature=0.2,
    # )