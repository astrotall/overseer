from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import lifespan
from libs.core.config import Settings
from libs.core.exceptions import ConfigurationError
from libs.tools.registry import ToolRegistry


def test_api_startup_fails_fast_when_active_provider_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key=None, anthropic_api_key=None)
    monkeypatch.setattr("apps.api.main.get_settings", lambda: settings)

    app = FastAPI(lifespan=lifespan)

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"), TestClient(app):
        pass


class FakeRedis:
    async def ping(self) -> bool:
        return True


class FakeLLMClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FailingToolRegistry(ToolRegistry):
    def register(self, tool: Any) -> None:
        raise ConfigurationError("boom")


def test_api_shutdown_closes_resources_when_tool_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key")
    monkeypatch.setattr("apps.api.main.get_settings", lambda: settings)
    monkeypatch.setattr("apps.api.main.validate_llm_provider_key", lambda _settings: None)
    monkeypatch.setattr("apps.api.main.configure_logging", lambda _settings: None)
    monkeypatch.setattr("apps.api.main.init_engine", lambda _settings: None)
    monkeypatch.setattr("apps.api.main.init_redis", lambda _settings: FakeRedis())

    llm_client = FakeLLMClient()
    monkeypatch.setattr("apps.api.main.get_llm_client", lambda _settings: llm_client)
    monkeypatch.setattr("apps.api.main.init_tool_registry", lambda: FailingToolRegistry())

    engine_closed = False
    redis_closed = False

    async def fake_close_engine() -> None:
        nonlocal engine_closed
        engine_closed = True

    async def fake_close_redis() -> None:
        nonlocal redis_closed
        redis_closed = True

    monkeypatch.setattr("apps.api.main.close_engine", fake_close_engine)
    monkeypatch.setattr("apps.api.main.close_redis", fake_close_redis)

    app = FastAPI(lifespan=lifespan)

    with pytest.raises(ConfigurationError, match="boom"), TestClient(app):
        pass

    assert llm_client.closed is True
    assert engine_closed is True
    assert redis_closed is True
