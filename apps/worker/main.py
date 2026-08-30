from __future__ import annotations

from typing import Any

from arq.connections import RedisSettings

from libs.core.config import get_settings
from libs.core.logging import configure_logging, get_logger
from libs.db.redis import close_redis, init_redis
from libs.db.session import close_engine, init_engine

logger = get_logger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    logger.info("worker.ping", job_id=ctx.get("job_id"))
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)
    init_redis(settings)
    logger.info("worker.startup", env=settings.env)


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis()
    await close_engine()
    logger.info("worker.shutdown")


class WorkerSettings:
    functions = [ping]
    cron_jobs: list[Any] = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 300
