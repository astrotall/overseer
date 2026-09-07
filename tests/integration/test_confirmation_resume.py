from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import ChatService, ConfirmationService, PendingConfirmationHandler
from libs.confirmations import ConfirmationRequiredError, ConfirmationStore
from libs.core.exceptions import NotFoundError
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
        self.deleted: list[str] = []

    async def _execute(self, arguments: DangerousArguments) -> ToolResult:
        self.deleted.append(arguments.path)
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
    history_limit: int | None = None,
) -> ChatService:
    kwargs: dict[str, Any] = {}
    if history_limit is not None:
        kwargs["history_limit"] = history_limit
    return ChatService(
        session,
        llm_client,
        tool_registry=_registry(*tools),
        confirmation_handler=PendingConfirmationHandler(store),
        **kwargs,
    )


DELETE_CALL = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})


async def _pause_on_delete(
    session: AsyncSession,
    store: ConfirmationStore,
    tool: DangerousTool,
    *,
    text: str = "удали отчёт",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Доводит диалог до паузы на `delete_file` и отдаёт (conversation_id, confirmation_id)."""
    conversation_id = await _new_conversation(session)
    llm_client = ScriptedLLMClient([_tool_use(DELETE_CALL, text="сейчас удалю")])

    with pytest.raises(ConfirmationRequiredError) as raised:
        await _service(session, llm_client, store, tool).send_message(conversation_id, text)

    return conversation_id, raised.value.confirmation_id


def _assert_history_is_well_formed(messages: Sequence[ChatMessage]) -> None:
    if not messages:
        return
    assert messages[0].role == "user"

    announced: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            announced.update(call.id for call in message.tool_calls)
        elif message.role == "tool":
            tool_call_id = message.tool_call_id
            assert tool_call_id is not None
            assert tool_call_id in announced
            answered.add(tool_call_id)

    assert announced == answered, "у каждого tool_use в срезе обязан быть парный tool_result"


@pytest.mark.integration
async def test_confirming_runs_the_tool_and_finishes_the_turn(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    conversation_id, confirmation_id = await _pause_on_delete(db_session, store, tool)

    resume_llm = ScriptedLLMClient([_final("Файл удалён.")])
    chat_service = _service(db_session, resume_llm, store, tool)

    answer = await ConfirmationService(store, chat_service).confirm(confirmation_id)

    assert answer == ChatMessage(role="assistant", content="Файл удалён.")
    assert tool.deleted == ["отчёт.docx"]

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert [call.id for call in history[1].tool_calls] == ["call-1"]
    assert history[2].tool_call_id == "call-1"
    assert history[2].is_error is False
    assert history[2].content is not None
    assert "отчёт.docx" in history[2].content
    _assert_history_is_well_formed(history)


@pytest.mark.integration
async def test_the_continuation_may_ask_for_another_tool(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    echo = EchoTool()
    conversation_id, confirmation_id = await _pause_on_delete(db_session, store, tool)

    echo_call = ToolCall(id="call-2", name="echo", arguments={"text": "готово"})
    resume_llm = ScriptedLLMClient([_tool_use(echo_call), _final("Удалил и повторил.")])
    chat_service = _service(db_session, resume_llm, store, tool, echo)

    answer = await ConfirmationService(store, chat_service).confirm(confirmation_id)

    assert answer == ChatMessage(role="assistant", content="Удалил и повторил.")
    assert tool.deleted == ["отчёт.docx"]

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [message.tool_call_id for message in (history[2], history[4])] == ["call-1", "call-2"]
    _assert_history_is_well_formed(history)


@pytest.mark.integration
async def test_the_continuation_may_pause_on_yet_another_confirmation(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Возобновление не одноразовое: продолжение идёт в обычный диспетчер OVE-22,
    и второй confirmation-required вызов останавливает ход ровно так же, как первый."""
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    conversation_id, first_id = await _pause_on_delete(db_session, store, tool)

    second_call = ToolCall(id="call-2", name="delete_file", arguments={"path": "черновик.docx"})
    resume_llm = ScriptedLLMClient([_tool_use(second_call)])
    chat_service = _service(db_session, resume_llm, store, tool)

    with pytest.raises(ConfirmationRequiredError) as raised:
        await ConfirmationService(store, chat_service).confirm(first_id)

    second_id = raised.value.confirmation_id
    assert second_id != first_id
    assert tool.deleted == ["отчёт.docx"]

    pending = await store.get_pending(second_id)
    assert pending.conversation_id == conversation_id
    assert pending.tool_call_id == "call-2"
    assert pending.arguments == {"path": "черновик.docx"}

    with pytest.raises(NotFoundError):
        await store.get_pending(first_id)

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool"]
    _assert_history_is_well_formed(history)

    await store.resolve_pending(second_id)


@pytest.mark.integration
async def test_rejecting_never_runs_the_tool(db_session: AsyncSession, redis_client: Redis) -> None:
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    conversation_id, confirmation_id = await _pause_on_delete(db_session, store, tool)

    resume_llm = ScriptedLLMClient([_final("Хорошо, не удаляю.")])
    chat_service = _service(db_session, resume_llm, store, tool)

    answer = await ConfirmationService(store, chat_service).reject(confirmation_id)

    assert answer == ChatMessage(role="assistant", content="Хорошо, не удаляю.")
    assert tool.deleted == []

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2].tool_call_id == "call-1"
    assert history[2].is_error is True
    assert history[2].content is not None
    assert "отклонил" in history[2].content
    _assert_history_is_well_formed(history)

    (continuation,) = resume_llm.calls
    assert [message.role for message in continuation] == ["system", "user", "assistant", "tool"]
    assert continuation[3].is_error is True


@pytest.mark.integration
@pytest.mark.parametrize("decision", ["confirm", "reject"])
async def test_resolving_the_same_confirmation_twice_is_an_explicit_error(
    db_session: AsyncSession, redis_client: Redis, decision: str
) -> None:
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    _, confirmation_id = await _pause_on_delete(db_session, store, tool)

    resume_llm = ScriptedLLMClient([_final("Готово.")])
    service = ConfirmationService(store, _service(db_session, resume_llm, store, tool))
    await service.confirm(confirmation_id)
    resolve = service.confirm if decision == "confirm" else service.reject

    with pytest.raises(NotFoundError):
        await resolve(confirmation_id)

    assert tool.deleted == ["отчёт.docx"]


@pytest.mark.integration
async def test_an_expired_or_unknown_confirmation_is_an_explicit_error(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    chat_service = _service(db_session, ScriptedLLMClient([]), store, DangerousTool())

    with pytest.raises(NotFoundError):
        await ConfirmationService(store, chat_service).confirm(uuid.uuid4())


@pytest.mark.integration
@pytest.mark.parametrize("history_limit", [1, 2, 3, 4, 5, 6])
async def test_the_resumed_history_slice_never_starts_mid_turn(
    db_session: AsyncSession, redis_client: Redis, history_limit: int
) -> None:
    """DoD OVE-26, проверенный настоящим потребителем: срез, снятый уже после возобновления,
    режет реальную историю с парой `tool_use` + `tool_result` внутри неё."""
    store = ConfirmationStore(redis_client)
    tool = DangerousTool()
    conversation_id = await _new_conversation(db_session)

    warmup = ScriptedLLMClient([_final("Привет.")])
    await _service(db_session, warmup, store, tool).send_message(conversation_id, "привет")

    pause_llm = ScriptedLLMClient([_tool_use(DELETE_CALL)])
    with pytest.raises(ConfirmationRequiredError) as raised:
        await _service(db_session, pause_llm, store, tool).send_message(
            conversation_id, "удали отчёт"
        )

    resume_llm = ScriptedLLMClient([_final("Удалил.")])
    chat_service = _service(db_session, resume_llm, store, tool, history_limit=history_limit)
    await ConfirmationService(store, chat_service).confirm(raised.value.confirmation_id)

    (continuation,) = resume_llm.calls
    assert continuation[0].role == "system"
    _assert_history_is_well_formed(continuation[1:])
    assert len(continuation) - 1 <= history_limit

    history = await ConversationRepository(db_session).get_history(conversation_id)
    _assert_history_is_well_formed(history)
    assert [message.role for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
