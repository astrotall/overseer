from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import (
    DEFAULT_HISTORY_LIMIT,
    TOOL_ROUNDS_EXHAUSTED_TEXT,
    ChatService,
)
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.llm.system_prompt import get_system_prompt_message
from libs.tools import EchoTool, Tool, ToolRegistry, ToolResult


class FakeLLMClient(LLMClient):
    default_model = "fake-model"

    def __init__(self, response: LLMResponse | None = None) -> None:
        self.calls: list[list[ChatMessage]] = []
        self.response = response or LLMResponse(
            model="fake-model", stop_reason="end_turn", text="готово"
        )

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
        return self.response


class FailingLLMClient(LLMClient):
    default_model = "fake-model"

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        raise RuntimeError("провайдер недоступен")


class ScriptedLLMClient(LLMClient):
    """Отдаёт на каждый вызов `complete()` следующий заскриптованный исход по очереди."""

    default_model = "fake-model"

    def __init__(self, outcomes: Sequence[LLMResponse | Exception]) -> None:
        self.calls: list[list[ChatMessage]] = []
        self.offered_tools: list[list[ToolSpec] | None] = []
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
        self.offered_tools.append(list(tools) if tools is not None else None)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _new_conversation(session: AsyncSession) -> uuid.UUID:
    conversation_id = await ConversationRepository(session).create_conversation()
    await session.commit()
    return conversation_id


@pytest.mark.integration
async def test_send_message_persists_turn_and_returns_answer(db_session: AsyncSession) -> None:
    conversation_id = await _new_conversation(db_session)
    llm_client = FakeLLMClient()
    service = ChatService(db_session, llm_client)

    answer = await service.send_message(conversation_id, "привет")

    assert answer == ChatMessage(role="assistant", content="готово")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history == [
        ChatMessage(role="user", content="привет"),
        ChatMessage(role="assistant", content="готово"),
    ]


@pytest.mark.integration
async def test_send_message_calls_llm_with_system_prompt_and_history(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)
    await repository.append_message(conversation_id, ChatMessage(role="user", content="кто ты"))
    await repository.append_message(
        conversation_id, ChatMessage(role="assistant", content="я Overseer")
    )
    llm_client = FakeLLMClient()

    await ChatService(db_session, llm_client).send_message(conversation_id, "открой отчёт")

    assert llm_client.calls == [
        [
            get_system_prompt_message(),
            ChatMessage(role="user", content="кто ты"),
            ChatMessage(role="assistant", content="я Overseer"),
            ChatMessage(role="user", content="открой отчёт"),
        ]
    ]


@pytest.mark.integration
async def test_system_prompt_is_not_persisted_into_history(db_session: AsyncSession) -> None:
    conversation_id = await _new_conversation(db_session)
    service = ChatService(db_session, FakeLLMClient())

    await service.send_message(conversation_id, "первый")
    await service.send_message(conversation_id, "второй")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "user", "assistant"]


@pytest.mark.integration
async def test_history_sent_to_llm_is_truncated_to_limit(db_session: AsyncSession) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)
    for index in range(6):
        await repository.append_message(
            conversation_id, ChatMessage(role="user", content=f"старое-{index}")
        )
    llm_client = FakeLLMClient()

    await ChatService(db_session, llm_client, history_limit=3).send_message(
        conversation_id, "новое"
    )

    assert llm_client.calls[0][1].role == "user"
    assert llm_client.calls[0] == [
        get_system_prompt_message(),
        ChatMessage(role="user", content="старое-4"),
        ChatMessage(role="user", content="старое-5"),
        ChatMessage(role="user", content="новое"),
    ]

    history = await repository.get_history(conversation_id)
    assert len(history) == 8


@pytest.mark.integration
async def test_history_sent_to_llm_with_default_limit_starts_with_user_message(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)
    for index in range(15):
        await repository.append_message(
            conversation_id, ChatMessage(role="user", content=f"вопрос-{index}")
        )
        await repository.append_message(
            conversation_id, ChatMessage(role="assistant", content=f"ответ-{index}")
        )
    llm_client = FakeLLMClient()

    await ChatService(db_session, llm_client).send_message(conversation_id, "новое")

    sent_messages = llm_client.calls[0]
    assert sent_messages[0] == get_system_prompt_message()
    assert len(sent_messages) == 1 + DEFAULT_HISTORY_LIMIT
    assert sent_messages[1].role == "user"
    assert sent_messages[-1] == ChatMessage(role="user", content="новое")


@pytest.mark.integration
async def test_unexpected_tool_calls_do_not_crash_the_turn(db_session: AsyncSession) -> None:
    conversation_id = await _new_conversation(db_session)
    llm_client = FakeLLMClient(
        LLMResponse(
            model="fake-model",
            stop_reason="tool_use",
            text="сейчас открою",
            tool_calls=[ToolCall(id="call-1", name="open_document", arguments={"path": "a.docx"})],
        )
    )

    answer = await ChatService(db_session, llm_client).send_message(conversation_id, "открой файл")

    assert answer == ChatMessage(role="assistant", content="сейчас открою")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history[-1] == ChatMessage(role="assistant", content="сейчас открою")
    assert history[-1].tool_calls == []


@pytest.mark.integration
async def test_failed_llm_call_persists_nothing(db_session: AsyncSession) -> None:
    conversation_id = await _new_conversation(db_session)
    service = ChatService(db_session, FailingLLMClient())

    with pytest.raises(RuntimeError, match="провайдер недоступен"):
        await service.send_message(conversation_id, "привет")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history == []


@pytest.mark.integration
async def test_non_positive_history_limit_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="history_limit"):
        ChatService(db_session, FakeLLMClient(), history_limit=0)

    assert DEFAULT_HISTORY_LIMIT > 0


@pytest.mark.integration
async def test_non_positive_max_tool_rounds_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="max_tool_rounds"):
        ChatService(db_session, FakeLLMClient(), max_tool_rounds=0)


@pytest.mark.integration
async def test_session_stays_usable_across_turns_after_a_rollback(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)

    ok_response = LLMResponse(model="fake-model", stop_reason="end_turn", text="готово")
    llm_client = ScriptedLLMClient([ok_response, RuntimeError("провайдер недоступен"), ok_response])
    service = ChatService(db_session, llm_client)

    first_answer = await service.send_message(conversation_id, "первый")
    assert first_answer == ChatMessage(role="assistant", content="готово")

    with pytest.raises(RuntimeError, match="провайдер недоступен"):
        await service.send_message(conversation_id, "упадёт")

    history_after_failure = await repository.get_history(conversation_id)
    assert history_after_failure == [
        ChatMessage(role="user", content="первый"),
        ChatMessage(role="assistant", content="готово"),
    ]

    second_answer = await service.send_message(conversation_id, "второй")
    assert second_answer == ChatMessage(role="assistant", content="готово")

    history_after_recovery = await repository.get_history(conversation_id)
    assert history_after_recovery == [
        ChatMessage(role="user", content="первый"),
        ChatMessage(role="assistant", content="готово"),
        ChatMessage(role="user", content="второй"),
        ChatMessage(role="assistant", content="готово"),
    ]
    assert ChatMessage(role="user", content="упадёт") not in history_after_recovery


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


def _tool_use(call: ToolCall, *, text: str = "") -> LLMResponse:
    return LLMResponse(model="fake-model", stop_reason="tool_use", text=text, tool_calls=[call])


def _final(text: str) -> LLMResponse:
    return LLMResponse(model="fake-model", stop_reason="end_turn", text=text)


async def _append_tool_turn(
    repository: ConversationRepository, conversation_id: uuid.UUID, index: int
) -> None:
    call = ToolCall(id=f"call-{index}", name="echo", arguments={"text": f"эхо-{index}"})
    await repository.append_message(
        conversation_id, ChatMessage(role="user", content=f"вопрос-{index}")
    )
    await repository.append_message(
        conversation_id,
        ChatMessage(role="assistant", content=f"сейчас-{index}", tool_calls=[call]),
    )
    await repository.append_message(
        conversation_id,
        ChatMessage(role="tool", content=f'{{"text": "эхо-{index}"}}', tool_call_id=call.id),
    )
    await repository.append_message(
        conversation_id, ChatMessage(role="assistant", content=f"ответ-{index}")
    )


async def _run_exhausted_tool_turn(
    session: AsyncSession, conversation_id: uuid.UUID, *, rounds: int, prefix: str
) -> None:
    """Ход, который кончился не ответом модели, а лимитом раундов."""
    llm_client = ScriptedLLMClient(
        [
            _tool_use(ToolCall(id=f"{prefix}-{index}", name="echo", arguments={"text": "ещё"}))
            for index in range(rounds + 1)
        ]
    )

    answer = await ChatService(
        session,
        llm_client,
        tool_registry=_registry(EchoTool()),
        max_tool_rounds=rounds,
    ).send_message(conversation_id, f"вопрос-{prefix}")

    assert answer == ChatMessage(role="assistant", content=TOOL_ROUNDS_EXHAUSTED_TEXT)


def _assert_history_is_well_formed(messages: Sequence[ChatMessage]) -> None:
    assert messages, "история не может быть пустой: свежее сообщение пользователя в ней всегда есть"
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
async def test_a_tool_call_is_executed_and_the_whole_turn_is_persisted(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})
    llm_client = ScriptedLLMClient([_tool_use(call, text="сейчас позову"), _final("готово")])
    echo = EchoTool()

    answer = await ChatService(db_session, llm_client, tool_registry=_registry(echo)).send_message(
        conversation_id, "скажи привет"
    )

    assert answer == ChatMessage(role="assistant", content="готово")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1].tool_calls == [call]
    assert history[2].tool_call_id == "call-1"
    assert history[2].is_error is False
    assert history[2].content is not None
    assert "привет" in history[2].content
    _assert_history_is_well_formed(history)


@pytest.mark.integration
async def test_the_second_llm_call_sees_the_tool_result_without_rereading_the_database(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})
    llm_client = ScriptedLLMClient([_tool_use(call), _final("готово")])
    registry = _registry(EchoTool())

    await ChatService(db_session, llm_client, tool_registry=registry).send_message(
        conversation_id, "скажи привет"
    )

    first, second = llm_client.calls
    assert [message.role for message in first] == ["system", "user"]
    assert [message.role for message in second] == ["system", "user", "assistant", "tool"]
    assert second[2].tool_calls == [call]
    assert second[3].tool_call_id == "call-1"
    assert llm_client.offered_tools == [registry.list_specs(), registry.list_specs()]


@pytest.mark.integration
async def test_a_model_that_always_wants_another_tool_call_ends_the_turn_cleanly(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    llm_client = ScriptedLLMClient(
        [
            _tool_use(ToolCall(id=f"call-{index}", name="echo", arguments={"text": "ещё"}))
            for index in range(20)
        ]
    )

    answer = await ChatService(
        db_session,
        llm_client,
        tool_registry=_registry(EchoTool()),
        max_tool_rounds=3,
    ).send_message(conversation_id, "зациклись")

    assert answer == ChatMessage(role="assistant", content=TOOL_ROUNDS_EXHAUSTED_TEXT)
    assert len(llm_client.calls) == 4

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[-1].tool_calls == []
    _assert_history_is_well_formed(history)


@pytest.mark.integration
@pytest.mark.parametrize("history_limit", list(range(1, 14)))
async def test_history_slice_never_starts_mid_turn(
    db_session: AsyncSession, history_limit: int
) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)
    for index in range(3):
        await _append_tool_turn(repository, conversation_id, index)
    await db_session.commit()
    llm_client = FakeLLMClient()

    await ChatService(db_session, llm_client, history_limit=history_limit).send_message(
        conversation_id, "новое"
    )

    sent = llm_client.calls[0]
    assert sent[0] == get_system_prompt_message()

    history = sent[1:]
    _assert_history_is_well_formed(history)
    assert history[-1] == ChatMessage(role="user", content="новое")
    assert len(history) <= history_limit


@pytest.mark.integration
@pytest.mark.parametrize("history_limit", list(range(1, 17)))
async def test_history_slice_never_starts_mid_turn_around_an_exhausted_turn(
    db_session: AsyncSession, history_limit: int
) -> None:
    conversation_id = await _new_conversation(db_session)
    repository = ConversationRepository(db_session)
    await _append_tool_turn(repository, conversation_id, 0)
    await db_session.commit()
    await _run_exhausted_tool_turn(db_session, conversation_id, rounds=2, prefix="лимит")
    await _append_tool_turn(repository, conversation_id, 1)
    await db_session.commit()

    persisted = await repository.get_history(conversation_id)
    assert [message.role for message in persisted] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert persisted[9] == ChatMessage(role="assistant", content=TOOL_ROUNDS_EXHAUSTED_TEXT)
    assert persisted[9].tool_calls == []

    llm_client = FakeLLMClient()
    await ChatService(db_session, llm_client, history_limit=history_limit).send_message(
        conversation_id, "новое"
    )

    sent = llm_client.calls[0]
    assert sent[0] == get_system_prompt_message()

    history = sent[1:]
    _assert_history_is_well_formed(history)
    assert history[-1] == ChatMessage(role="user", content="новое")
    assert len(history) <= history_limit


@pytest.mark.integration
async def test_a_tool_that_requires_confirmation_is_never_executed(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})
    llm_client = ScriptedLLMClient([_tool_use(call), _final("не стал удалять")])
    tool = DangerousTool()

    answer = await ChatService(db_session, llm_client, tool_registry=_registry(tool)).send_message(
        conversation_id, "удали отчёт"
    )

    assert tool.executed is False
    assert answer == ChatMessage(role="assistant", content="не стал удалять")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[2].is_error is True
    assert history[2].content is not None
    assert "подтверждени" in history[2].content
    _assert_history_is_well_formed(history)


@pytest.mark.integration
async def test_the_confirmation_seam_can_be_replaced_without_touching_the_dispatcher(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})
    llm_client = ScriptedLLMClient([_tool_use(call), _final("подтверждение запрошено")])
    tool = DangerousTool()
    asked: list[str] = []

    async def ask_the_user(pending: Tool[Any], pending_call: ToolCall) -> ToolResult:
        asked.append(pending.name)
        return ToolResult.ok(summary="Запрошено подтверждение", data={"call_id": pending_call.id})

    await ChatService(
        db_session,
        llm_client,
        tool_registry=_registry(tool),
        confirmation_handler=ask_the_user,
    ).send_message(conversation_id, "удали отчёт")

    assert asked == ["delete_file"]
    assert tool.executed is False

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history[2].is_error is False
    assert history[2].content is not None
    assert "call-1" in history[2].content


@pytest.mark.integration
async def test_a_tool_the_model_invented_comes_back_as_an_error_result(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="открой_ворд", arguments={})
    llm_client = ScriptedLLMClient([_tool_use(call), _final("такого инструмента нет")])

    answer = await ChatService(
        db_session, llm_client, tool_registry=_registry(EchoTool())
    ).send_message(conversation_id, "сделай что-нибудь")

    assert answer == ChatMessage(role="assistant", content="такого инструмента нет")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history[2].role == "tool"
    assert history[2].is_error is True
    assert history[2].content is not None
    assert "открой_ворд" in history[2].content


@pytest.mark.integration
async def test_a_failed_llm_call_mid_loop_persists_nothing_of_the_turn(
    db_session: AsyncSession,
) -> None:
    conversation_id = await _new_conversation(db_session)
    call = ToolCall(id="call-1", name="echo", arguments={"text": "привет"})
    llm_client = ScriptedLLMClient([_tool_use(call), RuntimeError("провайдер недоступен")])

    service = ChatService(db_session, llm_client, tool_registry=_registry(EchoTool()))
    with pytest.raises(RuntimeError, match="провайдер недоступен"):
        await service.send_message(conversation_id, "скажи привет")

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history == []
