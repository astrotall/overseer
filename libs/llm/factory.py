from __future__ import annotations

import threading

from libs.core.config import Settings, get_settings
from libs.llm.anthropic_client import AnthropicClient
from libs.llm.base import LLMClient
from libs.llm.deepseek_client import DeepSeekClient

_clients: dict[str, LLMClient] = {}
_clients_lock = threading.Lock()


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider
    with _clients_lock:
        client = _clients.get(provider)
        if client is None:
            client = _build_client(provider, settings)
            _clients[provider] = client
        return client


def _build_client(provider: str, settings: Settings) -> LLMClient:
    if provider == "anthropic":
        return AnthropicClient(settings)
    return DeepSeekClient(settings)


def reset_llm_client_cache() -> None:
    with _clients_lock:
        _clients.clear()
