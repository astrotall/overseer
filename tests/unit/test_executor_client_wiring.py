from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from apps.api.deps import ExecutorClientDep
from apps.api.main import lifespan
from libs.core.config import Settings
from libs.core.exceptions import ConfigurationError
from libs.executor import redis_client as executor_redis
from libs.executor.redis_client import (
    DEFAULT_POLL_SLICE_S,
    RedisExecutorClient,
    close_executor_client,
    get_executor_client,
    init_executor_client,
)
from libs.schemas.executor import MAX_CALL_WINDOW_S
from tests.conftest import TEST_API_TOKEN

UNREACHABLE_URL = "redis://default:not-a-secret@127.0.0.1:1/0"


def redis() -> Redis:
    return Redis.from_url(UNREACHABLE_URL)


@pytest.mark.parametrize(
    "options",
    [
        {"call_budget_s": MAX_CALL_WINDOW_S + 1},
        {"pickup_timeout_s": 10.0, "call_budget_s": 10.0},
        {"pickup_timeout_s": 0.0},
        {"poll_slice_s": 0.0},
        {"max_concurrent_calls": 0},
    ],
)
def test_impossible_timings_are_refused_at_construction(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        RedisExecutorClient(redis(), **options)


def test_the_client_from_settings_never_retries_and_outwaits_its_blocking_pops() -> None:
    settings = Settings(redis_url=UNREACHABLE_URL, executor_pickup_timeout_s=5.0)

    client = RedisExecutorClient.from_settings(settings)
    pool = client._redis.connection_pool
    connection = pool.make_connection()

    assert connection.retry._retries == 0
    assert connection.socket_timeout is not None
    assert connection.socket_timeout > max(DEFAULT_POLL_SLICE_S, 5.0)
    assert pool.max_connections == executor_redis.DEFAULT_MAX_CONCURRENT_CALLS
    assert client.call_budget_s == settings.executor_call_budget_s


async def test_the_process_client_must_be_initialized_before_use() -> None:
    await close_executor_client()
    with pytest.raises(ConfigurationError, match="init_executor_client"):
        get_executor_client()

    settings = Settings(redis_url=UNREACHABLE_URL)
    first = init_executor_client(settings)
    assert init_executor_client(settings) is first
    assert get_executor_client() is first

    await close_executor_client()
    with pytest.raises(ConfigurationError):
        get_executor_client()


def test_startup_creates_the_executor_client_without_talking_to_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        llm_provider="deepseek",
        deepseek_api_key="test-key",
        api_token=TEST_API_TOKEN,
        redis_url=UNREACHABLE_URL,
    )
    monkeypatch.setattr("apps.api.main.get_settings", lambda: settings)
    monkeypatch.setattr("apps.api.main.configure_logging", lambda _settings: None)
    monkeypatch.setattr("apps.api.main.init_engine", lambda _settings: None)

    class FakeRedis:
        async def ping(self) -> bool:
            return True

    class FakeLLMClient:
        async def aclose(self) -> None:
            return None

    async def noop() -> None:
        return None

    monkeypatch.setattr("apps.api.main.init_redis", lambda _settings: FakeRedis())
    monkeypatch.setattr("apps.api.main.get_llm_client", lambda _settings: FakeLLMClient())
    monkeypatch.setattr("apps.api.main.close_engine", noop)
    monkeypatch.setattr("apps.api.main.close_redis", noop)

    app = FastAPI(lifespan=lifespan)

    @app.get("/probe")
    def probe(client: ExecutorClientDep) -> dict[str, str]:
        return {"client": type(client).__name__}

    with TestClient(app) as http:
        assert http.get("/probe").json() == {"client": "RedisExecutorClient"}

    with pytest.raises(ConfigurationError):
        get_executor_client()


def test_a_missing_token_stops_startup_before_the_executor_client_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key="test-key", api_token=None)
    monkeypatch.setattr("apps.api.main.get_settings", lambda: settings)

    def untouchable(_settings: Settings) -> None:
        raise AssertionError("клиент executor не строится, пока токен не проверен")

    monkeypatch.setattr("apps.api.main.init_executor_client", untouchable)

    app = FastAPI(lifespan=lifespan)
    with pytest.raises(ConfigurationError, match="API_TOKEN"), TestClient(app):
        pass
