from __future__ import annotations

import pytest

from libs.core.config import Settings
from libs.llm.anthropic_client import AnthropicClient
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
