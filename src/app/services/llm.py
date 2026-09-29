import asyncio
import random

import httpx

# from langchain_ollama import ChatOllama
from langchain_mistralai import ChatMistralAI

try:  # provider-agnostic connection failures; name moved between langchain-core versions
    from langchain_core.exceptions import APIConnectionError as _LCConnectionError
except ImportError:  # pragma: no cover - older/newer langchain-core
    _LCConnectionError = None

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


class ModelTransientError(RuntimeError):
    """Transient provider failure that survived every retry.

    Callers catch this to degrade gracefully (re-prompt, best-effort output)
    instead of letting a network blip abort a multi-minute build.
    """


class MistralCapacityError(ModelTransientError):
    """Raised when Mistral's backend stays out of capacity across retries."""


class ModelTransportError(ModelTransientError):
    """Raised when the provider kept timing out / dropping the connection."""


# Transport-level failures where no response ever arrived, so retrying is safe.
# NOTE httpx.ReadTimeout is a TransportError but *not* a RequestError, which is
# why langchain-mistralai's own retry decorator lets it escape and it used to
# kill the whole build loop.
_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
)
if _LCConnectionError is not None:  # pragma: no branch
    _TRANSIENT_TRANSPORT_ERRORS += (_LCConnectionError,)


async def ainvoke_with_capacity_retries(callable, messages, retries: int | None = None):
    """Invoke ``callable.ainvoke(messages)``, retrying transient failures.

    Two classes of transient failure are retried with exponential backoff + jitter:

    1. Capacity 429s. Mistral returns ``backend_out_of_capacity`` (code 3505)
       HTTP 429 when the serving backend is momentarily saturated.
       langchain-mistralai's built-in retry decorator only retries
       ``httpx.RequestError``/``StreamError`` and deliberately lets
       ``HTTPStatusError`` (which 429 raises) through, so without this wrapper a
       capacity blip aborts the whole build/plan stream.
    2. Transport failures — read timeouts, dropped connections, connect errors.
       These carry no response, so retrying is always safe.

    Any other status (401/403/404/5xx…) re-raises immediately. When retries run
    out, raises a :class:`ModelTransientError` subclass so callers can degrade
    gracefully instead of propagating a raw httpx traceback.
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
        except _TRANSIENT_TRANSPORT_ERRORS as e:
            if attempt == retries:
                raise ModelTransportError(
                    f"Model provider kept failing after {retries + 1} attempts: "
                    f"{type(e).__name__}: {e}"
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