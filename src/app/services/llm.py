from langchain_ollama import ChatOllama

from app.config import get_settings

_OLLAMA_MODEL = "nemotron-3-ultra:cloud"
_OLLAMA_BASE_URL = "https://ollama.com"


def create_model() -> ChatOllama:
    """Create the application's chat model (Ollama cloud, temperature-tuned).

    The single provider touchpoint — swapping backends touches only here.
    """
    settings = get_settings()
    return ChatOllama(
        model=_OLLAMA_MODEL,
        base_url=_OLLAMA_BASE_URL,
        client_kwargs={
            "headers": {"Authorization": f"Bearer {settings.ollama_api_key}"}
        },
        temperature=0.2,
    )