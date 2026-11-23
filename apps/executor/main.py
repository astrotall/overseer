from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Final

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from apps.executor.config import (
    ExecutorSettings,
    get_executor_settings,
    redis_target,
    require_redis_url,
)
from apps.executor.consumer import ExecutorConsumer, ExecutorService
from apps.executor.dispatch import ExcelOperations, ExecutorDispatcher
from apps.executor.excel.com_backend import Win32ExcelBackend
from apps.executor.excel.controller import ExcelController
from apps.executor.excel.sessions import ConversationWorkbooks
from apps.executor.lease import ExecutorLease
from apps.executor.paths import build_path_policy, platform_resolver
from libs.core.exceptions import ConfigurationError
from libs.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

SOCKET_TIMEOUT_S: Final = 10.0
CONNECT_TIMEOUT_S: Final = 2.0
MAX_CONNECTIONS: Final = 4

REDIS_UNREACHABLE_TEXT: Final = (
    "Redis {host}:{port} не принял подключение пользователя {user} ({error}): проверь "
    "EXECUTOR_REDIS_URL, REDIS_EXECUTOR_PASSWORD и что Redis запущен"
)


def connect(settings: ExecutorSettings) -> Redis:
    return Redis.from_url(
        require_redis_url(settings),
        decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT_S,
        socket_connect_timeout=CONNECT_TIMEOUT_S,
        retry=Retry(NoBackoff(), 0),
        max_connections=MAX_CONNECTIONS,
    )


def build_dispatcher(settings: ExecutorSettings) -> ExecutorDispatcher:
    paths = build_path_policy(settings.allowed_roots, resolver=platform_resolver())
    workbooks = ConversationWorkbooks(
        ExcelController(Win32ExcelBackend()),
        max_conversations=settings.max_conversations,
        idle_ttl_s=settings.conversation_idle_ttl_s,
    )
    return ExecutorDispatcher(excel=ExcelOperations(workbooks, paths=paths))


def build_service(
    settings: ExecutorSettings, redis: Redis, dispatcher: ExecutorDispatcher
) -> ExecutorService:
    lease = ExecutorLease(
        redis,
        operations=dispatcher.operations,
        ttl_s=settings.heartbeat_ttl_s,
        interval_s=settings.heartbeat_interval_s,
    )
    consumer = ExecutorConsumer(
        redis,
        dispatcher,
        lease,
        reply_ttl_s=settings.reply_ttl_s,
        dedup_ttl_s=settings.dedup_ttl_s,
        max_call_s=settings.max_call_s,
    )
    return ExecutorService(consumer, lease, dispatcher)


def install_stop_signals(service: ExecutorService) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signum, service.request_stop)


async def run(settings: ExecutorSettings) -> None:
    target = redis_target(settings)
    if not target.restricted:
        logger.warning("executor.unrestricted_redis_user", user=target.user)
    redis = connect(settings)
    try:
        try:
            await redis.ping()
        except (RedisError, OSError) as exc:
            raise ConfigurationError(
                REDIS_UNREACHABLE_TEXT.format(
                    host=target.host, port=target.port, user=target.user, error=type(exc).__name__
                )
            ) from None
        dispatcher = build_dispatcher(settings)
        service = build_service(settings, redis, dispatcher)
        install_stop_signals(service)
        logger.info(
            "executor.starting",
            redis_host=target.host,
            redis_port=target.port,
            redis_user=target.user,
            operations=sorted(dispatcher.operations),
        )
        await service.run()
    finally:
        await redis.aclose()


def main() -> None:
    settings = get_executor_settings()
    configure_logging(level=settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        try:
            asyncio.run(run(settings))
        except ConfigurationError as exc:
            logger.error("executor.stopped", reason=str(exc))
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
