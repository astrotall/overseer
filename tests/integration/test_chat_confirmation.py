from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import ChatService, PendingConfirmationHandler
from libs.confirmations import ConfirmationRequiredError, ConfirmationStore
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.tools import EchoTool, Tool, ToolRegistry, ToolResult


class ScriptedLLMClient(LLMClient):
    """Отдаёт на каждый вызов `complete()` следующий заскриптованный ответ по очереди."""

    default_model = "fake-model"

    def __init__(self, outcomes: Sequence[LLMResponse]) -> None:
        self.calls: list[list[ChatMessage]] = []
        self._outcomes = list(outcomes)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return self._outcomes.pop(0)


class DangerousArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class DangerousTool(Tool[DangerousArguments]):
    name = "delete_file"
    description = "Удаляет файл — необратимое действие, требует подтверждения."
    arguments_model = DangerousArguments
    requires_confirmation = True

    def __init__(self) -> None:
        self.executed = False

    async def _execute(self, arguments: DangerousArguments) -> ToolResult:
        self.executed = True
        return ToolResult.ok(summary=f"Файл {arguments.path} удалён")


def _registry(*tools: Tool[Any]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _tool_use(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(
        model="fake-model", stop_reason="tool_use", text=text, tool_calls=list(calls)
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(model="fake-model", stop_reason="end_turn", text=text)


async def _new_conversation(session: AsyncSession) -> uuid.UUID:
    conversation_id = await ConversationRepository(session).create_conversation()
    await session.commit()
    return conversation_id


def _service(
    session: AsyncSession,
    llm_client: LLMClient,
    store: ConfirmationStore,
    *tools: Tool[Any],
) -> ChatService:
    return ChatService(
        session,
        llm_client,
        tool_registry=_registry(*tools),
        confirmation_handler=PendingConfirmationHandler(store),
    )


@pytest.mark.integration
async def test_a_call_to_a_tool_that_requires_confirmation_pauses_the_turn(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})
    tool = DangerousTool()
    store = ConfirmationStore(redis_client)
    llm_client = ScriptedLLMClient([_tool_use(call)])

    with pytest.raises(ConfirmationRequiredError) as raised:
        await _service(db_session, llm_client, store, tool).send_message(
            conversation_id, "удали отчёт"
        )

    assert tool.executed is False

    pending = await store.get_pending(raised.value.confirmation_id)
    assert pending.conversation_id == conversation_id
    assert pending.tool_call_id == "call-1"
    assert pending.tool_name == "delete_file"
    assert pending.arguments == {"path": "отчёт.docx"}
    assert pending.summary == raised.value.summary
    assert "delete_file" in raised.value.summary
    assert "отчёт.docx" in raised.value.summary

    await store.resolve_pending(raised.value.confirmation_id)


@pytest.mark.integration
async def test_a_paused_turn_leaves_only_the_user_message_in_the_database(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})
    store = ConfirmationStore(redis_client)
    llm_client = ScriptedLLMClient([_tool_use(call, text="сейчас удалю")])

    with pytest.raises(ConfirmationRequiredError) as raised:
        await _service(db_session, llm_client, store, DangerousTool()).send_message(
            conversation_id, "удали отчёт"
        )

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history == [ChatMessage(role="user", content="удали отчёт")]
    assert all(message.tool_calls == [] for message in history)

    await store.resolve_pending(raised.value.confirmation_id)


@pytest.mark.integration
async def test_a_pause_after_a_completed_round_keeps_that_round_paired(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    conversation_id = await _new_conversation(db_session)
    echo_call = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})
    delete_call = ToolCall(id="call-2", name="delete_file", arguments={"path": "отчёт.docx"})
    store = ConfirmationStore(redis_client)
    llm_client = ScriptedLLMClient([_tool_use(echo_call), _tool_use(delete_call)])

    with pytest.raises(ConfirmationRequiredError) as raised:
        await _service(db_session, llm_client, store, EchoTool(), DangerousTool()).send_message(
            conversation_id, "повтори и удали"
        )

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool"]
    assert [call.id for call in history[1].tool_calls] == ["call-1"]
    assert history[2].tool_call_id == "call-1"

    await store.resolve_pending(raised.value.confirmation_id)


@pytest.mark.integration
async def test_a_tool_without_confirmation_still_runs_inside_the_same_turn(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})
    store = ConfirmationStore(redis_client)
    llm_client = ScriptedLLMClient([_tool_use(call), _final("повторил")])

    answer = await _service(db_session, llm_client, store, EchoTool()).send_message(
        conversation_id, "повтори привет"
    )

    assert answer == ChatMessage(role="assistant", content="повторил")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2].tool_call_id == "call-1"
    assert history[2].is_error is False
    assert history[2].content is not None
    assert "привет" in history[2].content
