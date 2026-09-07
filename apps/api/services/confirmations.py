from __future__ import annotations

import uuid
from typing import Any

from apps.api.services.chat import ChatService
from libs.confirmations import ConfirmationRequiredError, ConfirmationStore
from libs.core.logging import get_logger
from libs.llm.base import ChatMessage, ToolCall
from libs.tools import Tool, ToolResult

logger = get_logger(__name__)


class PendingConfirmationHandler:
    def __init__(self, store: ConfirmationStore) -> None:
        self._store = store

    async def __call__(
        self, conversation_id: uuid.UUID, tool: Tool[Any], call: ToolCall
    ) -> ToolResult:
        summary = tool.describe_call(call.arguments)
        confirmation_id = await self._store.create_pending(
            conversation_id,
            tool_call_id=call.id,
            tool_name=tool.name,
            arguments=call.arguments,
            summary=summary,
        )
        logger.info(
            "chat.confirmation_pending_created",
            conversation_id=str(conversation_id),
            tool=tool.name,
            tool_call_id=call.id,
            confirmation_id=str(confirmation_id),
        )
        raise ConfirmationRequiredError(confirmation_id, summary)


REJECTED_BY_USER_TEXT = (
    "Пользователь отклонил вызов инструмента {tool}: действие не выполнено. "
    "Не повторяй тот же вызов — сообщи об отказе и предложи другой путь."
)


class ConfirmationService:
    """Возобновление хода, приостановленного на подтверждении (OVE-27).

    Порядок шагов важен и вынесен сюда целиком, а не спрятан в `ChatService`:
    инструмент исполняется, раунд коммитится, и только после этого `PendingConfirmation`
    удаляется из стора — сбой персиста оставляет ожидающее подтверждение на месте."""

    def __init__(self, store: ConfirmationStore, chat_service: ChatService) -> None:
        self._store = store
        self._chat_service = chat_service

    async def confirm(self, confirmation_id: uuid.UUID) -> ChatMessage:
        return await self._resume(confirmation_id, approved=True)

    async def reject(self, confirmation_id: uuid.UUID) -> ChatMessage:
        return await self._resume(confirmation_id, approved=False)

    async def _resume(self, confirmation_id: uuid.UUID, *, approved: bool) -> ChatMessage:
        pending = await self._store.get_pending(confirmation_id)
        call = ToolCall(
            id=pending.tool_call_id, name=pending.tool_name, arguments=pending.arguments
        )

        if approved:
            result = await self._chat_service.execute_confirmed_call(pending.conversation_id, call)
        else:
            result = ToolResult.failed(REJECTED_BY_USER_TEXT.format(tool=call.name))

        await self._chat_service.persist_resumed_round(pending.conversation_id, call, result)
        await self._store.resolve_pending(confirmation_id)

        logger.info(
            "chat.confirmation_resolved",
            conversation_id=str(pending.conversation_id),
            confirmation_id=str(confirmation_id),
            tool=call.name,
            tool_call_id=call.id,
            approved=approved,
        )
        return await self._chat_service.continue_turn(pending.conversation_id)
