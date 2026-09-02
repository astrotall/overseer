from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import DEFAULT_HISTORY_LIMIT, ChatService
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.llm.system_prompt import get_system_prompt_message


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
async def test_even_history_limit_is_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="history_limit"):
        ChatService(db_session, FakeLLMClient(), history_limit=4)

    assert DEFAULT_HISTORY_LIMIT % 2 == 1


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
