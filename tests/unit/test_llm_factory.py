from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from libs.core.config import Settings
from libs.llm import factory
from libs.llm.anthropic_client import AnthropicClient
from libs.llm.base import LLMClient
from libs.llm.deepseek_client import DeepSeekClient
from libs.llm.factory import get_llm_client, reset_llm_client_cache


@pytest.fixture(autouse=True)
def _reset_llm_client_cache() -> None:
    reset_llm_client_cache()
    yield
    reset_llm_client_cache()


def test_get_llm_client_returns_deepseek_by_default() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")

    client = get_llm_client(settings)

    assert isinstance(client, DeepSeekClient)


def test_get_llm_client_returns_anthropic_when_selected() -> None:
    settings = Settings(llm_provider="anthropic", anthropic_api_key="test-key")

    client = get_llm_client(settings)

    assert isinstance(client, AnthropicClient)


def test_get_llm_client_reuses_same_instance_for_same_provider() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")

    first = get_llm_client(settings)
    second = get_llm_client(settings)

    assert first is second


def test_get_llm_client_caches_providers_independently() -> None:
    deepseek_settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")
    anthropic_settings = Settings(llm_provider="anthropic", anthropic_api_key="test-key")

    deepseek_client = get_llm_client(deepseek_settings)
    anthropic_client = get_llm_client(anthropic_settings)

    assert deepseek_client is not anthropic_client
    assert get_llm_client(deepseek_settings) is deepseek_client


def test_reset_llm_client_cache_forces_new_instance() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")

    first = get_llm_client(settings)
    reset_llm_client_cache()
    second = get_llm_client(settings)

    assert first is not second


async def test_get_llm_client_builds_client_once_under_concurrent_first_calls() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")
    original_build_client = factory._build_client
    build_calls_lock = threading.Lock()
    build_calls = 0

    def _slow_build_client(provider: str, settings: Settings) -> LLMClient:
        nonlocal build_calls
        with build_calls_lock:
            build_calls += 1
        time.sleep(0.05)
        return original_build_client(provider, settings)

    with patch.object(factory, "_build_client", side_effect=_slow_build_client) as mock_build:
        results = await asyncio.gather(
            *(asyncio.to_thread(get_llm_client, settings) for _ in range(8))
        )

    assert mock_build.call_count == 1
    assert build_calls == 1
    assert len({id(client) for client in results}) == 1
