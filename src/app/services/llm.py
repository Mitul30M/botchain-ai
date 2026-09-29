import asyncio
import random

import httpx

# from langchain_ollama import ChatOllama
from langchain_mistralai import ChatMistralAI

from app.config import get_settings

# --- Ollama (commented out 2026-09-28 while evaluating Mistral; swap back by
#     uncommenting the import + create_model body below and restoring the
#     Ollama return) ---
# _OLLAMA_MODEL = "gemma4:cloud"
# _OLLAMA_BASE_URL = "https://ollama.com"

# Mistral — available models on the free API tier (verified 2026-09-28):
#   ministral-3b/8b/14b-latest -> 200 (~188 req/min), clean tool_calls
#   open-mistral-nemo          -> 200 but deprecated (retire 7/31/2026)
#   mistral-small/medium-latest -> 429 (0 req/min), mistral-large-latest -> 403
#   (gated behind a plan upgrade — would be Small 4 / Medium 3.5 / Large 3)
_MISTRAL_MODEL = "ministral-14b-latest"

# Public name of the model in use — stored on each Chat.model at creation so the
# UI shows the real model instead of the DB's "claude-sonnet-4-6" placeholder.
MODEL_NAME = _MISTRAL_MODEL


class MistralCapacityError(RuntimeError):
    """Raised when Mistral's backend stays out of capacity across retries."""


async def ainvoke_with_capacity_retries(callable, messages, retries: int | None = None):
    """Invoke ``callable.ainvoke(messages)``, retrying transient capacity 429s.

    Mistral returns ``backend_out_of_capacity`` (code 3505) HTTP 429 responses
    when the serving backend is momentarily saturated. langchain-mistralai's
    built-in retry decorator only retries ``httpx.RequestError``/``StreamError``
    and deliberately lets ``HTTPStatusError`` (which 429 raises) through, so
    without this wrapper a capacity blip aborts the whole build/plan stream.

    Only capacity/overload 429s are retried (with exponential backoff + jitter);
    any other status (401/403/404/5xx…) re-raises immediately. When retries run
    out, raises ``MistralCapacityError`` so callers can degrade gracefully.
    """
    if retries is None:
        retries = get_settings().mistral_capacity_retries
    for attempt in range(retries + 1):
        try:
            return await callable.ainvoke(messages)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:
                raise
            if attempt == retries:
                raise MistralCapacityError(
                    "Mistral backend still out of capacity after retries."
                ) from e
            backoff = min(1.5 * (2**attempt), 10) + random.uniform(0, 0.5)
            await asyncio.sleep(backoff)
    raise MistralCapacityError(  # pragma: no cover - defensive
        "Mistral backend still out of capacity after retries."
    )


def create_model() -> ChatMistralAI:
    """Create the application's chat model (Mistral, temperature-tuned).

    The single provider touchpoint — swapping backends touches only here.
    """
    settings = get_settings()
    return ChatMistralAI(
        model=_MISTRAL_MODEL,
        api_key=settings.mistral_api_key or None,
        temperature=0.2,
        timeout=600,
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