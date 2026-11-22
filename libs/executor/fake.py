from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from libs.core.exceptions import OfficeError
from libs.schemas.executor import (
    ExecutorContractError,
    ExecutorErrorCode,
    ExecutorRequest,
    ExecutorResponse,
    encode_reply,
    encode_request,
    error_code_for,
    parse_reply,
    parse_request,
)

ExecutorHandler = Callable[[ExecutorRequest], Awaitable[ExecutorResponse]]
Clock = Callable[[], datetime]


async def acknowledge(request: ExecutorRequest) -> ExecutorResponse:
    return ExecutorResponse.ok(request.id)


def utc_now() -> datetime:
    return datetime.now(UTC)


class FakeExecutorClient:
    def __init__(
        self,
        handler: ExecutorHandler = acknowledge,
        *,
        running: bool = True,
        clock: Clock = utc_now,
    ) -> None:
        self.handler = handler
        self.running = running
        self.failure: ExecutorErrorCode | None = None
        self.requests: list[ExecutorRequest] = []
        self._clock = clock
        self._seen: set[uuid.UUID] = set()

    async def call(self, request: ExecutorRequest) -> ExecutorResponse:
        if not self.running:
            return ExecutorResponse.failed(request.id, "executor_unavailable")
        if self.failure is not None:
            return ExecutorResponse.failed(request.id, self.failure)

        try:
            delivered = parse_request(encode_request(request))
        except ExecutorContractError as exc:
            return ExecutorResponse.failed(request.id, exc.code)

        if delivered.id in self._seen:
            return ExecutorResponse.failed(request.id, "reply_timeout")
        self._seen.add(delivered.id)

        if self._clock() >= delivered.deadline:
            return ExecutorResponse.failed(request.id, "deadline_exceeded")

        self.requests.append(delivered)
        response = await self._handle(delivered)
        if response.id != delivered.id:
            return ExecutorResponse.failed(request.id, "reply_timeout")

        try:
            reply = parse_reply(encode_reply(response))
        except ExecutorContractError:
            return ExecutorResponse.failed(request.id, "malformed_reply")
        if not isinstance(reply, ExecutorResponse):
            return ExecutorResponse.failed(request.id, "malformed_reply")
        return reply

    async def _handle(self, request: ExecutorRequest) -> ExecutorResponse:
        try:
            return await self.handler(request)
        except OfficeError as exc:
            return ExecutorResponse.failed(request.id, error_code_for(exc))
        except Exception:
            return ExecutorResponse.failed(request.id, "internal_error")
