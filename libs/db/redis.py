from __future__ import annotations

from redis.asyncio import Redis, from_url

from libs.core.config import Settings, get_settings
from libs.core.exceptions import ConfigurationError

_redis: Redis | None = None


def init_redis(settings: Settings | None = None) -> Redis:
    global _redis

    if _redis is not None:
        return _redis

    settings = settings or get_settings()
    _redis = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis

    if _redis is not None:
        await _redis.aclose()
    _redis = None


def get_redis() -> Redis:
    if _redis is None:
        raise ConfigurationError("Redis не инициализирован: вызовите init_redis()")
    return _redis
