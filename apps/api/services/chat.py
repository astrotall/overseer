from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from libs.core.exceptions import NotFoundError
from libs.core.logging import get_logger
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.llm.system_prompt import get_system_prompt_message
from libs.tools import Tool, ToolRegistry, ToolResult

logger = get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 29
DEFAULT_MAX_TOOL_ROUNDS = 10

TOOL_ROUNDS_EXHAUSTED_TEXT = (
    "Не получилось закончить действие: за один ход исчерпан лимит вызовов инструментов. "
    "Сформулируйте задачу точнее или разбейте её на шаги."
)

ConfirmationHandler = Callable[[Tool[Any], ToolCall], Awaitable[ToolResult]]


async def confirmation_is_unavailable(tool: Tool[Any], call: ToolCall) -> ToolResult:
    return ToolResult.failed(
        f"Инструмент {tool.name} требует подтверждения пользователя, а механизм подтверждения "
        "ещё не реализован: вызов не выполнен. Скажи об этом пользователю и предложи путь "
        "без этого инструмента."
    )


def cut_to_turn_boundary(history: Sequence[ChatMessage]) -> list[ChatMessage]:
    for index, message in enumerate(history):
        if message.role == "user":
            return list(history[index:])
    return []


def _to_tool_message(call: ToolCall, result: ToolResult) -> ChatMessage:
    return ChatMessage(
        role="tool",
        content=result.model_dump_json(exclude_none=True),
        tool_call_id=call.id,
        is_error=result.is_error,
    )


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient,
        *,
        tool_registry: ToolRegistry | None = None,
        confirmation_handler: ConfirmationHandler = confirmation_is_unavailable,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        if history_limit <= 0:
            raise ValueError(f"history_limit must be positive, got {history_limit}")
        if max_tool_rounds <= 0:
            raise ValueError(f"max_tool_rounds must be positive, got {max_tool_rounds}")

        self._session = session
        self._repository = ConversationRepository(session)
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self._confirmation_handler = confirmation_handler
        self._history_limit = history_limit
        self._max_tool_rounds = max_tool_rounds

    async def send_message(self, conversation_id: uuid.UUID, text: str) -> ChatMessage:
        try:
            await self._repository.append_message(
                conversation_id, ChatMessage(role="user", content=text)
            )
            history = await self._load_history(conversation_id)
            answer = await self._run_turn(conversation_id, history)
            await self._session.commit()
            return answer
        except Exception:
            await self._session.rollback()
            raise

    async def _load_history(self, conversation_id: uuid.UUID) -> list[ChatMessage]:
        history = await self._repository.get_history(conversation_id, limit=self._history_limit)
        trimmed = cut_to_turn_boundary(history)
        if len(trimmed) != len(history):
            logger.info(
                "chat.history_cut_to_turn_boundary",
                conversation_id=str(conversation_id),
                dropped=len(history) - len(trimmed),
                kept=len(trimmed),
            )
        return trimmed

    async def _run_turn(
        self, conversation_id: uuid.UUID, history: Sequence[ChatMessage]
    ) -> ChatMessage:
        conversation: list[ChatMessage] = [get_system_prompt_message(), *history]
        specs = self._tool_specs()
        rounds = 0

        while True:
            response = await self._llm_client.complete(conversation, tools=specs or None)

            if not response.has_tool_calls:
                answer = response.to_message()
                break

            if not specs:
                logger.warning(
                    "chat.tool_calls_dropped",
                    conversation_id=str(conversation_id),
                    tools=[call.name for call in response.tool_calls],
                )
                answer = ChatMessage(role="assistant", content=response.text)
                break

            if rounds >= self._max_tool_rounds:
                logger.warning(
                    "chat.tool_rounds_exhausted",
                    conversation_id=str(conversation_id),
                    rounds=rounds,
                    tools=[call.name for call in response.tool_calls],
                )
                answer = ChatMessage(role="assistant", content=TOOL_ROUNDS_EXHAUSTED_TEXT)
                break

            rounds += 1
            await self._dispatch_tool_calls(conversation_id, conversation, response)

        await self._repository.append_message(conversation_id, answer)
        self._log_turn_completed(conversation_id, response, history, rounds)
        return answer

    async def _dispatch_tool_calls(
        self,
        conversation_id: uuid.UUID,
        conversation: list[ChatMessage],
        response: LLMResponse,
    ) -> None:
        requested = response.to_message()
        await self._repository.append_message(conversation_id, requested)
        conversation.append(requested)

        for call in response.tool_calls:
            result = await self._invoke(conversation_id, call)
            tool_message = _to_tool_message(call, result)
            await self._repository.append_message(conversation_id, tool_message)
            conversation.append(tool_message)

    async def _invoke(self, conversation_id: uuid.UUID, call: ToolCall) -> ToolResult:
        assert self._tool_registry is not None

        try:
            tool = self._tool_registry.get(call.name)
        except NotFoundError:
            logger.warning(
                "chat.tool_not_found",
                conversation_id=str(conversation_id),
                tool=call.name,
            )
            return ToolResult.failed(
                f"Инструмента '{call.name}' не существует. Выбери инструмент из списка доступных."
            )

        if tool.requires_confirmation:
            logger.info(
                "chat.tool_confirmation_required",
                conversation_id=str(conversation_id),
                tool=tool.name,
                tool_call_id=call.id,
            )
            result = await self._confirmation_handler(tool, call)
        else:
            result = await tool.execute(call.arguments)

        logger.info(
            "chat.tool_call_finished",
            conversation_id=str(conversation_id),
            tool=tool.name,
            tool_call_id=call.id,
            status=result.status,
        )
        return result

    def _tool_specs(self) -> list[ToolSpec]:
        if self._tool_registry is None:
            return []
        return self._tool_registry.list_specs()

    def _log_turn_completed(
        self,
        conversation_id: uuid.UUID,
        response: LLMResponse,
        history: Sequence[ChatMessage],
        rounds: int,
    ) -> None:
        logger.info(
            "chat.turn_completed",
            conversation_id=str(conversation_id),
            model=response.model,
            stop_reason=response.stop_reason,
            history_size=len(history),
            tool_rounds=rounds,
        )
