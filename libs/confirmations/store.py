from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from libs.core.exceptions import ConflictError, NotFoundError

DEFAULT_TTL_SECONDS = 10 * 60

_KEY_PREFIX = "overseer:confirmation:pending:"
_CLAIM_PREFIX = "overseer:confirmation:claim:"

_CLAIM_NOT_FOUND = 0
_CLAIM_ACQUIRED = 1
_CLAIM_CONFLICT = 2

_CLAIM_SCRIPT = f"""
local pending = redis.call('GET', KEYS[1])
if not pending then
    return {{{_CLAIM_NOT_FOUND}}}
end
if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[1]) then
    return {{{_CLAIM_ACQUIRED}, pending}}
end
return {{{_CLAIM_CONFLICT}}}
"""


class PendingConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    summary: str


class ConfirmationStore:
    def __init__(self, redis: Redis, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._claim_script: AsyncScript = redis.register_script(_CLAIM_SCRIPT)

    async def create_pending(
        self,
        conversation_id: uuid.UUID,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> uuid.UUID:
        confirmation_id = uuid.uuid4()
        pending = PendingConfirmation(
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            summary=summary,
        )
        await self._redis.set(
            _key(confirmation_id),
            pending.model_dump_json(),
            ex=self._ttl_seconds,
        )
        return confirmation_id

    async def get_pending(self, confirmation_id: uuid.UUID) -> PendingConfirmation:
        raw = await self._redis.get(_key(confirmation_id))
        if raw is None:
            raise NotFoundError(f"Подтверждение '{confirmation_id}' не найдено или истекло")
        return PendingConfirmation.model_validate_json(raw)

    async def claim_pending(self, confirmation_id: uuid.UUID) -> PendingConfirmation:
        outcome: list[Any] = await self._claim_script(
            keys=[_key(confirmation_id), _claim_key(confirmation_id)],
            args=[self._ttl_seconds],
        )
        status = int(outcome[0])
        if status == _CLAIM_NOT_FOUND:
            raise NotFoundError(f"Подтверждение '{confirmation_id}' не найдено или истекло")
        if status == _CLAIM_CONFLICT:
            raise ConflictError(
                f"Подтверждение '{confirmation_id}' уже обрабатывается другим запросом"
            )
        return PendingConfirmation.model_validate_json(outcome[1])

    async def resolve_pending(self, confirmation_id: uuid.UUID) -> None:
        await self._redis.delete(_key(confirmation_id))


def _key(confirmation_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{confirmation_id}"


def _claim_key(confirmation_id: uuid.UUID) -> str:
    return f"{_CLAIM_PREFIX}{confirmation_id}"
