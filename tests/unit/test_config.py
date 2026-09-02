from __future__ import annotations

import pytest

from libs.core.config import Settings, validate_llm_provider_key
from libs.core.exceptions import ConfigurationError


def test_validate_llm_provider_key_passes_when_active_provider_key_is_set() -> None:
    settings = Settings(
        llm_provider="deepseek", deepseek_api_key="test-key", anthropic_api_key=None
    )

    validate_llm_provider_key(settings)


def test_validate_llm_provider_key_raises_when_deepseek_key_missing() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key=None)

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        validate_llm_provider_key(settings)


def test_validate_llm_provider_key_raises_when_anthropic_key_missing() -> None:
    settings = Settings(llm_provider="anthropic", anthropic_api_key=None)

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        validate_llm_provider_key(settings)


def test_validate_llm_provider_key_passes_when_anthropic_key_is_set() -> None:
    settings = Settings(
        llm_provider="anthropic", anthropic_api_key="test-key", deepseek_api_key=None
    )

    validate_llm_provider_key(settings)
