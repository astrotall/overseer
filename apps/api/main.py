from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routes import api_router
from libs.core.config import get_settings, validate_llm_provider_key
from libs.core.logging import configure_logging, get_logger
from libs.db.redis import close_redis, init_redis
from libs.db.session import close_engine, init_engine
from libs.llm.factory import get_llm_client, reset_llm_client_cache

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    validate_llm_provider_key(settings)
    configure_logging(settings)

    init_engine(settings)
    redis = init_redis(settings)
    await redis.ping()
    llm_client = get_llm_client(settings)
    logger.info("api.startup", env=settings.env, llm_provider=settings.llm_provider)

    try:
        yield
    finally:
        await llm_client.aclose()
        reset_llm_client_cache()
        await close_redis()
        await close_engine()
        logger.info("api.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Overseer API",
        version="0.1.0",
        description="Системный ИИ-агент: REST + WebSocket интерфейс",
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
