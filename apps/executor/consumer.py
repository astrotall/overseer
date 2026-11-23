from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Protocol

import structlog
from pydantic import JsonValue
from redis.asyncio import Redis
from redis.exceptions import RedisError

from apps.executor.lease import OWNERSHIP_LOST_TEXT, ExecutorLease, OwnershipLostError
from libs.core.exceptions import OfficeError
from libs.core.logging import get_logger
from libs.executor.commands import blpop
from libs.executor.keys import REQUESTS_KEY, reply_key, seen_key
from libs.schemas.executor import (
    ExecutorAccepted,
    ExecutorContractError,
    ExecutorErrorCode,
    ExecutorReply,
    ExecutorRequest,
    ExecutorResponse,
    encode_reply,
    error_code_for,
    parse_request,
)

logger = get_logger(__name__)

TRANSPORT_FAILURES: Final = (RedisError, OSError)
POLL_TIMEOUT_S: Final = 1.0
TRANSPORT_BACKOFF_S: Final = 1.0
CANCELLED_REPLY_TIMEOUT_S: Final = 2.0

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class RequestRefusedError(Exception):
    def __init__(self, code: ExecutorErrorCode) -> None:
        super().__init__(code)
        self.code: ExecutorErrorCode = code


class Dispatcher(Protocol):
    @property
    def operations(self) -> frozenset[str]: ...

    async def start(self) -> None: ...

    async def dispatch(self, request: ExecutorRequest) -> dict[str, JsonValue]: ...

    async def idle(self) -> None: ...

    async def aclose(self) -> None: ...


class ExecutorConsumer:
    def __init__(
        self,
        redis: Redis,
        dispatcher: Dispatcher,
        lease: ExecutorLease,
        *,
        reply_ttl_s: int,
        dedup_ttl_s: float,
        max_call_s: float,
        poll_timeout_s: float = POLL_TIMEOUT_S,
        clock: Clock = utc_now,
    ) -> None:
        self._redis = redis
        self._dispatcher = dispatcher
        self._lease = lease
        self._reply_ttl_s = reply_ttl_s
        self._dedup_ttl_ms = int(dedup_ttl_s * 1000)
        self._max_call_s = max_call_s
        self._poll_timeout_s = poll_timeout_s
        self._clock = clock
        self._stopping = asyncio.Event()
        self.served = 0

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set() and not self._lease.lost:
            confirmed = self._lease.confirmed_for()
            if confirmed <= 0:
                await self._pause(self._poll_timeout_s)
                continue

            try:
                raw = await blpop(self._redis, REQUESTS_KEY, min(self._poll_timeout_s, confirmed))
            except TRANSPORT_FAILURES as exc:
                logger.warning("executor.queue_unavailable", error=type(exc).__name__)
                await self._pause(TRANSPORT_BACKOFF_S)
                continue

            if raw is None:
                await self._idle()
                continue

            try:
                await self._serve(raw)
            except Exception:
                logger.exception("executor.serve_failed")
            self.served += 1

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _idle(self) -> None:
        try:
            await self._dispatcher.idle()
        except Exception:
            logger.exception("executor.idle_failed")

    async def _serve(self, raw: str) -> None:
        try:
            request = parse_request(raw)
        except ExecutorContractError as exc:
            logger.warning(
                "executor.request_rejected",
                code=exc.code,
                errors=list(exc.errors),
                request_id=None if exc.request_id is None else str(exc.request_id),
                payload_bytes=len(raw.encode("utf-8", "replace")),
            )
            if exc.request_id is not None:
                await self._reply(ExecutorResponse.failed(exc.request_id, exc.code))
            return

        log = logger.bind(
            request_id=str(request.id),
            conversation_id=str(request.conversation_id),
            operation=request.operation,
        )
        fresh = await self._redis.set(seen_key(request.id), "1", nx=True, px=self._dedup_ttl_ms)
        if not fresh:
            log.warning("executor.duplicate_request")
            return
        if self._clock() >= request.deadline:
            log.warning("executor.deadline_exceeded")
            await self._reply(ExecutorResponse.failed(request.id, "deadline_exceeded"))
            return

        await self._reply(ExecutorAccepted(id=request.id))
        started = time.monotonic()
        try:
            response = await self._execute(request, log)
        except asyncio.CancelledError:
            log.warning("executor.call_interrupted")
            with contextlib.suppress(*TRANSPORT_FAILURES, TimeoutError):
                async with asyncio.timeout(CANCELLED_REPLY_TIMEOUT_S):
                    await self._reply(ExecutorResponse.failed(request.id, "internal_error"))
            raise
        await self._reply(response)
        error = response.error
        log.info(
            "executor.call_served",
            status=response.status,
            code=None if error is None else error.code,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _execute(
        self, request: ExecutorRequest, log: structlog.stdlib.BoundLogger
    ) -> ExecutorResponse:
        try:
            async with asyncio.timeout(self._max_call_s) as scope:
                data = await self._dispatcher.dispatch(request)
        except TimeoutError:
            if not scope.expired():
                log.exception("executor.call_failed")
                return ExecutorResponse.failed(request.id, "internal_error")
            log.warning("executor.call_timed_out", limit_s=self._max_call_s)
            return ExecutorResponse.failed(request.id, "timeout")
        except RequestRefusedError as exc:
            log.warning("executor.request_refused", code=exc.code)
            return ExecutorResponse.failed(request.id, exc.code)
        except OfficeError as exc:
            code = error_code_for(exc)
            log.warning("executor.office_error", code=code, error=type(exc).__name__)
            return ExecutorResponse.failed(request.id, code)
        except Exception:
            log.exception("executor.call_failed")
            return ExecutorResponse.failed(request.id, "internal_error")
        return ExecutorResponse.ok(request.id, data)

    async def _reply(self, reply: ExecutorReply) -> None:
        try:
            payload = encode_reply(reply)
        except ExecutorContractError:
            logger.error("executor.reply_too_large", request_id=str(reply.id))
            payload = encode_reply(ExecutorResponse.failed(reply.id, "internal_error"))

        key = reply_key(reply.id)
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.rpush(key, payload)
        pipeline.expire(key, math.ceil(self._reply_ttl_s))
        await pipeline.execute()


class ExecutorService:
    def __init__(
        self,
        consumer: ExecutorConsumer,
        lease: ExecutorLease,
        dispatcher: Dispatcher,
        *,
        acquire_wait_s: float | None = None,
    ) -> None:
        self._consumer = consumer
        self._lease = lease
        self._dispatcher = dispatcher
        self._acquire_wait_s = acquire_wait_s

    def request_stop(self) -> None:
        logger.info("executor.stopping")
        self._consumer.request_stop()

    async def run(self) -> None:
        await self._lease.acquire(wait_s=self._acquire_wait_s)
        try:
            await self._dispatcher.start()
            await self._serve()
        finally:
            await self._lease.release()
            await self._dispatcher.aclose()
            logger.info("executor.stopped", served=self._consumer.served)
        if self._lease.lost:
            raise OwnershipLostError(OWNERSHIP_LOST_TEXT)

    def _on_keeper_done(self, keeper: asyncio.Task[None]) -> None:
        if keeper.cancelled():
            return
        error = keeper.exception()
        if error is not None:
            logger.error("executor.heartbeat_crashed", error=type(error).__name__)
        self.request_stop()

    async def _serve(self) -> None:
        keeper = asyncio.create_task(self._lease.keep_alive(), name="executor-heartbeat")
        consumer = asyncio.create_task(self._consumer.run(), name="executor-consumer")
        keeper.add_done_callback(self._on_keeper_done)
        try:
            try:
                await asyncio.shield(consumer)
            except asyncio.CancelledError:
                if consumer.done():
                    raise
                self.request_stop()
                await consumer
                raise
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
