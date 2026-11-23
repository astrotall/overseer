from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import structlog
from redis.asyncio import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from libs.core.config import Settings, get_settings
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.executor.client import DEFAULT_CALL_BUDGET_S, DEFAULT_PICKUP_TIMEOUT_S, ExecutorClient
from libs.executor.commands import blpop, lrem, rpush
from libs.executor.keys import HEARTBEAT_KEY, REQUESTS_KEY, reply_key
from libs.schemas.executor import (
    MAX_CALL_WINDOW_S,
    ExecutorAccepted,
    ExecutorContractError,
    ExecutorErrorCode,
    ExecutorHeartbeat,
    ExecutorRequest,
    ExecutorResponse,
    encode_request,
    parse_heartbeat,
    parse_reply,
)

logger = get_logger(__name__)

DEFAULT_POLL_SLICE_S: Final = 5.0
DEFAULT_MAX_CONCURRENT_CALLS: Final = 4
SOCKET_MARGIN_S: Final = 5.0
CONNECT_TIMEOUT_S: Final = 2.0
WITHDRAW_TIMEOUT_S: Final = 2.0
CLOCK_SKEW_WARNING_S: Final = 5.0
MIN_BLOCK_S: Final = 0.01

TRANSPORT_FAILURES: Final = (RedisError, OSError)

Clock = Callable[[], datetime]

NOT_INITIALIZED_TEXT: Final = (
    "Клиент executor не инициализирован: вызовите init_executor_client() в lifespan"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RedisExecutorClient:
    def __init__(
        self,
        redis: Redis,
        *,
        pickup_timeout_s: float = DEFAULT_PICKUP_TIMEOUT_S,
        call_budget_s: float = DEFAULT_CALL_BUDGET_S,
        poll_slice_s: float = DEFAULT_POLL_SLICE_S,
        max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
        clock: Clock = utc_now,
        owns_redis: bool = False,
    ) -> None:
        if pickup_timeout_s <= 0 or poll_slice_s <= 0 or max_concurrent_calls < 1:
            raise ConfigurationError(
                "Таймаут пикапа, срез ожидания и число одновременных вызовов executor "
                "должны быть положительными"
            )
        if not pickup_timeout_s < call_budget_s <= MAX_CALL_WINDOW_S:
            raise ConfigurationError(
                "Бюджет вызова executor должен быть больше таймаута пикапа и не больше "
                f"{MAX_CALL_WINDOW_S:g} с — окна, которое принимает контракт"
            )

        self._redis = redis
        self._pickup_timeout_s = pickup_timeout_s
        self._call_budget_s = call_budget_s
        self._poll_slice_s = poll_slice_s
        self._slots = asyncio.Semaphore(max_concurrent_calls)
        self._clock = clock
        self._owns_redis = owns_redis

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisExecutorClient:
        pickup = settings.executor_pickup_timeout_s
        pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=DEFAULT_MAX_CONCURRENT_CALLS,
            socket_timeout=max(pickup, DEFAULT_POLL_SLICE_S) + SOCKET_MARGIN_S,
            socket_connect_timeout=CONNECT_TIMEOUT_S,
            retry=Retry(NoBackoff(), 0),
            decode_responses=True,
        )
        return cls(
            Redis(connection_pool=pool),
            pickup_timeout_s=pickup,
            call_budget_s=settings.executor_call_budget_s,
            owns_redis=True,
        )

    @property
    def call_budget_s(self) -> float:
        return self._call_budget_s

    async def aclose(self) -> None:
        if self._owns_redis:
            await self._redis.aclose(close_connection_pool=True)

    async def call(self, request: ExecutorRequest) -> ExecutorResponse:
        log = logger.bind(
            request_id=str(request.id),
            conversation_id=str(request.conversation_id),
            operation=request.operation,
        )
        started = time.monotonic()
        response = await self._call(request, log)
        error = response.error
        log.info(
            "executor.call_finished",
            status=response.status,
            code=None if error is None else error.code,
            outcome=None if error is None else error.outcome,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return response

    async def _call(
        self, request: ExecutorRequest, log: structlog.stdlib.BoundLogger
    ) -> ExecutorResponse:
        try:
            payload = encode_request(request)
        except ExecutorContractError as exc:
            log.warning("executor.request_too_large")
            return ExecutorResponse.failed(request.id, exc.code)

        budget = min((request.deadline - self._clock()).total_seconds(), self._call_budget_s)
        if budget <= 0:
            log.warning("executor.deadline_passed_before_send")
            return ExecutorResponse.failed(request.id, "deadline_exceeded")
        deadline = time.monotonic() + budget

        try:
            async with asyncio.timeout(min(self._pickup_timeout_s, budget)):
                await self._slots.acquire()
        except TimeoutError:
            log.warning("executor.not_picked_up", reason="client_busy")
            return ExecutorResponse.failed(request.id, "not_picked_up")

        try:
            connection = self._redis.client()
            try:
                await connection.initialize()
                exchange = _Exchange(
                    connection,
                    request,
                    payload,
                    deadline=deadline,
                    pickup_timeout_s=self._pickup_timeout_s,
                    poll_slice_s=self._poll_slice_s,
                    clock=self._clock,
                    log=log,
                )
                return await exchange.run()
            finally:
                await connection.aclose()
        except TRANSPORT_FAILURES as exc:
            log.warning("executor.transport_unavailable", error=type(exc).__name__)
            return ExecutorResponse.failed(request.id, "transport_unavailable")
        finally:
            self._slots.release()


class _Exchange:
    def __init__(
        self,
        connection: Redis,
        request: ExecutorRequest,
        payload: str,
        *,
        deadline: float,
        pickup_timeout_s: float,
        poll_slice_s: float,
        clock: Clock,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        self._connection = connection
        self._request = request
        self._payload = payload
        self._reply_key = reply_key(request.id)
        self._deadline = deadline
        self._pickup_timeout_s = pickup_timeout_s
        self._poll_slice_s = poll_slice_s
        self._clock = clock
        self._log = log
        self._instance_id: uuid.UUID | None = None
        self._accepted = False
        self._taken = False

    async def run(self) -> ExecutorResponse:
        failure = await self._check_heartbeat()
        if failure is not None:
            return self._failed(failure)

        try:
            await rpush(self._connection, REQUESTS_KEY, self._payload)
        except TRANSPORT_FAILURES as exc:
            return await self._after_lost_send(exc, stage="push")
        self._log.debug("executor.request_sent", payload_bytes=len(self._payload.encode()))

        try:
            return await self._await_result()
        except asyncio.CancelledError:
            await self._withdraw_on_cancel()
            raise

    async def _check_heartbeat(self) -> ExecutorErrorCode | None:
        try:
            raw = await self._connection.get(HEARTBEAT_KEY)
        except TRANSPORT_FAILURES as exc:
            self._log.warning("executor.transport_unavailable", error=type(exc).__name__)
            return "transport_unavailable"

        if raw is None:
            self._log.warning("executor.unavailable")
            return "executor_unavailable"

        try:
            heartbeat = parse_heartbeat(raw)
        except ExecutorContractError as exc:
            if exc.code == "unsupported_version":
                self._log.warning("executor.version_mismatch", errors=list(exc.errors))
            else:
                self._log.warning("executor.unavailable", reason="malformed_heartbeat")
            return exc.code

        if self._request.operation not in heartbeat.operations:
            self._log.warning("executor.unknown_operation")
            return "unknown_operation"

        self._warn_on_clock_skew(heartbeat)
        self._instance_id = heartbeat.instance_id
        return None

    def _warn_on_clock_skew(self, heartbeat: ExecutorHeartbeat) -> None:
        ahead = (heartbeat.sent_at - self._clock()).total_seconds()
        if ahead > CLOCK_SKEW_WARNING_S:
            self._log.warning("executor.clock_skew", executor_ahead_s=round(ahead, 1))

    async def _await_result(self) -> ExecutorResponse:
        pickup_wait = min(self._pickup_timeout_s, self._remaining())
        try:
            raw = await self._pop(pickup_wait)
        except TRANSPORT_FAILURES as exc:
            return await self._after_lost_send(exc, stage="pickup")

        if raw is None:
            try:
                removed = await lrem(self._connection, REQUESTS_KEY, self._payload)
            except TRANSPORT_FAILURES as exc:
                self._log.warning("executor.transport_error", error=type(exc).__name__)
                return self._failed("transport_error")
            if removed:
                self._log.warning("executor.not_picked_up", reason="pickup_timeout")
                return self._failed("not_picked_up")
            self._taken = True
        else:
            outcome = self._read(raw)
            if outcome is not None:
                return outcome

        return await self._await_after_pickup()

    async def _await_after_pickup(self) -> ExecutorResponse:
        while (remaining := self._remaining()) > 0:
            try:
                raw = await self._pop(min(self._poll_slice_s, remaining))
                if raw is None:
                    if not await self._executor_alive():
                        self._log.warning("executor.reply_timeout", reason="executor_lost")
                        return self._failed("reply_timeout")
                    continue
            except TRANSPORT_FAILURES as exc:
                self._log.warning("executor.transport_error", error=type(exc).__name__)
                return self._failed("transport_error")

            outcome = self._read(raw)
            if outcome is not None:
                return outcome

        self._log.warning("executor.reply_timeout", reason="deadline")
        return self._failed("reply_timeout")

    def _read(self, raw: str) -> ExecutorResponse | None:
        try:
            reply = parse_reply(raw)
        except ExecutorContractError as exc:
            self._log.warning("executor.malformed_reply", errors=list(exc.errors))
            return self._failed("malformed_reply")

        if reply.id != self._request.id:
            self._log.warning("executor.malformed_reply", reason="foreign_id")
            return self._failed("malformed_reply")
        if isinstance(reply, ExecutorAccepted):
            self._accepted = True
            self._taken = True
            return None
        return reply

    async def _pop(self, block_s: float) -> str | None:
        return await blpop(self._connection, self._reply_key, max(block_s, MIN_BLOCK_S))

    async def _executor_alive(self) -> bool:
        raw = await self._connection.get(HEARTBEAT_KEY)
        if raw is None:
            return False
        try:
            heartbeat = parse_heartbeat(raw)
        except ExecutorContractError:
            return False
        return heartbeat.instance_id == self._instance_id

    async def _after_lost_send(self, exc: BaseException, *, stage: str) -> ExecutorResponse:
        self._log.warning("executor.transport_error", stage=stage, error=type(exc).__name__)
        removed = await self._try_withdraw()
        if removed:
            return self._failed("transport_unavailable")
        return self._failed("transport_error")

    async def _withdraw_on_cancel(self) -> None:
        if self._taken:
            self._log.warning("executor.call_cancelled", withdrawn=False, taken=True)
            return
        removed = await self._try_withdraw()
        self._log.warning("executor.call_cancelled", withdrawn=removed, taken=not removed)

    async def _try_withdraw(self) -> bool:
        try:
            async with asyncio.timeout(WITHDRAW_TIMEOUT_S):
                removed = await lrem(self._connection, REQUESTS_KEY, self._payload)
        except (*TRANSPORT_FAILURES, TimeoutError):
            return False
        return bool(removed)

    def _remaining(self) -> float:
        return self._deadline - time.monotonic()

    def _failed(self, code: ExecutorErrorCode) -> ExecutorResponse:
        return ExecutorResponse.failed(self._request.id, code)


_client: RedisExecutorClient | None = None


def init_executor_client(settings: Settings | None = None) -> RedisExecutorClient:
    global _client

    if _client is None:
        _client = RedisExecutorClient.from_settings(settings or get_settings())
    return _client


def get_executor_client() -> ExecutorClient:
    if _client is None:
        raise ConfigurationError(NOT_INITIALIZED_TEXT)
    return _client


async def close_executor_client() -> None:
    global _client

    client, _client = _client, None
    if client is not None:
        await client.aclose()
