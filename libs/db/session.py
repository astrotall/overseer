from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from libs.core.config import Settings, get_settings
from libs.core.exceptions import ConfigurationError

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _sessionmaker

    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    _engine = create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        future=True,
    )
    _sessionmaker = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return _engine


async def close_engine() -> None:
    global _engine, _sessionmaker

    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise ConfigurationError("Engine не инициализирован: вызовите init_engine()")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise ConfigurationError("Sessionmaker не инициализирован: вызовите init_engine()")
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
