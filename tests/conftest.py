from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from apps.api.main import app as fastapi_app
from libs.core.config import Settings, get_settings
from libs.db import models


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
        pytest.skip(f"Тестовая БД недоступна ({test_database_url}): {exc}")

    yield engine
    await engine.dispose()


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


@pytest.fixture
def app() -> Iterator[FastAPI]:
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def async_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
