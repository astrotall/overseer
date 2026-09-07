from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

from libs.core.exceptions import NotFoundError

DEFAULT_TTL_SECONDS = 10 * 60

_KEY_PREFIX = "overseer:confirmation:pending:"


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

    async def resolve_pending(self, confirmation_id: uuid.UUID) -> None:
        await self._redis.delete(_key(confirmation_id))


def _key(confirmation_id: uuid.UUID) -> str:
    return f"{_KEY_PREFIX}{confirmation_id}"
