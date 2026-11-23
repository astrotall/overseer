from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis, from_url
from redis.exceptions import AuthenticationError, RedisError, ResponseError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from apps.api.deps import get_confirmation_store
from apps.api.main import app as fastapi_app
from apps.executor.config import ExecutorSettings, get_executor_settings
from apps.voice.config import VoiceSettings
from libs.confirmations import ConfirmationStore
from libs.core.config import Settings, get_settings
from libs.db import models
from libs.llm import ToolCall
from libs.schemas.ws import ws_auth_subprotocols
from libs.tools import init_tool_registry, reset_tool_registry

TEST_API_TOKEN = "overseer-test-token-not-a-secret-0123456789"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
WS_AUTH_SUBPROTOCOLS = ws_auth_subprotocols(TEST_API_TOKEN)


UNIT_TESTS_DIR = Path(__file__).parent / "unit"
HERMETIC_SETTINGS_FIXTURE = "hermetic_settings"


SettingsClass = type[Settings] | type[VoiceSettings] | type[ExecutorSettings]
HERMETIC_SETTINGS_CLASSES: tuple[SettingsClass, ...] = (Settings, VoiceSettings, ExecutorSettings)


def _alias_names(alias: object) -> set[str]:
    if isinstance(alias, str):
        return {alias.lower()}
    choices = getattr(alias, "choices", ())
    return {choice.lower() for choice in choices if isinstance(choice, str)}


def _settings_env_names(settings_cls: SettingsClass) -> set[str]:
    prefix = str(settings_cls.model_config.get("env_prefix", "")).lower()
    names: set[str] = set()
    for name, info in settings_cls.model_fields.items():
        names.add(f"{prefix}{name}")
        names |= _alias_names(info.validation_alias)
    return names


SETTINGS_ENV_NAMES = set().union(*(_settings_env_names(cls) for cls in HERMETIC_SETTINGS_CLASSES))


def _needs_hermetic_settings(item: pytest.Item) -> bool:
    return (
        isinstance(item, pytest.Function)
        and item.path.is_relative_to(UNIT_TESTS_DIR)
        and HERMETIC_SETTINGS_FIXTURE not in item.fixturenames
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="требует Windows и live-сессии пользователя (apps/executor)")
    for item in items:
        if sys.platform != "win32" and item.get_closest_marker("windows") is not None:
            item.add_marker(skip)
        if isinstance(item, pytest.Function) and _needs_hermetic_settings(item):
            item.fixturenames.insert(0, HERMETIC_SETTINGS_FIXTURE)


@pytest.fixture
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key)
    for settings_cls in HERMETIC_SETTINGS_CLASSES:
        monkeypatch.setitem(settings_cls.model_config, "env_file", None)
    get_settings.cache_clear()
    get_executor_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_executor_settings.cache_clear()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def test_database_url(settings: Settings) -> str:
    if settings.database_url_test:
        return settings.database_url_test

    url = make_url(settings.database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


REDIS_LOGICAL_DATABASES = 16


@pytest.fixture(scope="session")
def test_redis_url(settings: Settings) -> str:
    if settings.redis_url_test:
        return settings.redis_url_test

    parts = urlsplit(settings.redis_url)
    db = int(parts.path.lstrip("/") or "0")
    test_db = (db + 1) % REDIS_LOGICAL_DATABASES
    return urlunsplit(parts._replace(path=f"/{test_db}"))


def redact_database_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def database_unavailable_reason(url: str, exc: BaseException) -> str:
    password = make_url(url).password
    detail = str(exc)
    if password:
        detail = detail.replace(str(password), "***")
    return f"Тестовая БД недоступна ({redact_database_url(url)}): {detail}"


def redact_redis_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.password is None:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{parts.username or ''}:***@{host}"))


@pytest.fixture(scope="session")
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, poolclass=NullPool, future=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(models.Base.metadata.create_all)
    except (SQLAlchemyError, OSError) as exc:
        await engine.dispose()
        if os.getenv("CI"):
            raise
        pytest.skip(database_unavailable_reason(test_database_url, exc))

    yield engine
    await engine.dispose()


CLUSTER_MODE_SELECT_ERROR_MARKER = "SELECT is not allowed in cluster mode"


class RedisClusterModeNotSupportedError(RuntimeError):
    """Raised when the derived test database can't be selected because Redis runs in
    cluster mode (or another setup that only supports logical database 0)."""


def _raise_if_cluster_mode_select_error(exc: ResponseError, *, redis_url_test: str | None) -> None:
    if redis_url_test:
        return
    if CLUSTER_MODE_SELECT_ERROR_MARKER not in str(exc):
        return
    raise RedisClusterModeNotSupportedError(
        "REDIS_URL_TEST must be set explicitly - the configured Redis does not support "
        "multiple logical databases (cluster mode)"
    ) from exc


@pytest.fixture(scope="session")
async def redis_client(test_redis_url: str, settings: Settings) -> AsyncIterator[Redis]:
    client: Redis = from_url(test_redis_url, encoding="utf-8", decode_responses=True)
    shown_url = redact_redis_url(test_redis_url)
    try:
        await client.ping()
    except AuthenticationError as exc:
        await client.aclose()
        pytest.fail(
            f"Redis ({shown_url}) отклонил учётные данные ({exc}): Redis требует пароль "
            "(OVE-59) — сверь REDIS_URL / REDIS_URL_TEST в .env с .env.example и REDIS_PASSWORD"
        )
    except ResponseError as exc:
        await client.aclose()
        _raise_if_cluster_mode_select_error(exc, redis_url_test=settings.redis_url_test)
        if os.getenv("CI"):
            raise
        pytest.skip(f"Redis недоступен ({shown_url}): {exc}")
    except RedisError as exc:
        await client.aclose()
        if os.getenv("CI"):
            raise
        pytest.skip(f"Redis недоступен ({shown_url}): {exc}")

    yield client
    await client.aclose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session:
            yield session
        await transaction.rollback()


class UnreachableRedis:
    """Заглушка Redis для приложения в тестах: до неё не должна доходить ни одна команда.

    Тест, которому нужен настоящий store, подменяет `get_confirmation_store` сам."""

    async def set(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("тест не ожидал обращения к Redis")

    async def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("тест не ожидал обращения к Redis")

    async def delete(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("тест не ожидал обращения к Redis")

    def register_script(self, script: str | bytes) -> Callable[..., Awaitable[None]]:
        async def unreachable(*args: object, **kwargs: object) -> None:
            raise AssertionError("тест не ожидал обращения к Redis")

        return unreachable


def authenticated_settings(**overrides: object) -> Settings:
    return get_settings().model_copy(
        update={"api_token": SecretStr(TEST_API_TOKEN), "api_allowed_origins": [], **overrides}
    )


@pytest.fixture
def app() -> Iterator[FastAPI]:
    init_tool_registry()
    fastapi_app.dependency_overrides[get_settings] = lambda: authenticated_settings()
    fastapi_app.dependency_overrides[get_confirmation_store] = lambda: ConfirmationStore(
        UnreachableRedis()  # type: ignore[arg-type]  # заглушка вместо клиента Redis
    )
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()
    reset_tool_registry()


@pytest.fixture
async def async_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AUTH_HEADERS
    ) as client:
        yield client


@pytest.fixture
async def anonymous_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _assert_tool_calls_conform_to_contract(tool_calls: Sequence[ToolCall]) -> None:
    for call in tool_calls:
        assert isinstance(call, ToolCall)
        assert isinstance(call.id, str)
        assert call.id
        assert isinstance(call.name, str)
        assert call.name
        assert isinstance(call.arguments, dict)
        assert all(isinstance(key, str) for key in call.arguments)


@pytest.fixture
def assert_tool_calls_conform_to_contract() -> Callable[[Sequence[ToolCall]], None]:
    return _assert_tool_calls_conform_to_contract
