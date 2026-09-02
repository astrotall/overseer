from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from libs.db.models import Conversation
from libs.db.repositories import ConversationRepository
from libs.llm.base import ChatMessage, ToolCall


@pytest.mark.integration
async def test_create_conversation(db_session: AsyncSession) -> None:
    repository = ConversationRepository(db_session)

    conversation = await repository.create_conversation()

    assert conversation.id is not None
    assert conversation.title is None


@pytest.mark.integration
async def test_append_and_get_history_round_trips_text_messages(db_session: AsyncSession) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    await repository.append_message(
        conversation.id, ChatMessage(role="system", content="be concise")
    )
    await repository.append_message(conversation.id, ChatMessage(role="user", content="hi"))
    await repository.append_message(conversation.id, ChatMessage(role="assistant", content="hello"))

    history = await repository.get_history(conversation.id)

    assert history == [
        ChatMessage(role="system", content="be concise"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]


@pytest.mark.integration
async def test_append_and_get_history_round_trips_assistant_tool_calls(
    db_session: AsyncSession,
) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    tool_call = ToolCall(id="call-1", name="search", arguments={"query": "overseer"})
    assistant_message = ChatMessage(role="assistant", content=None, tool_calls=[tool_call])
    await repository.append_message(conversation.id, assistant_message)

    history = await repository.get_history(conversation.id)

    assert history == [assistant_message]


@pytest.mark.integration
async def test_append_and_get_history_round_trips_tool_result_messages(
    db_session: AsyncSession,
) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    tool_message = ChatMessage(role="tool", content="boom", tool_call_id="call-1", is_error=True)
    await repository.append_message(conversation.id, tool_message)

    history = await repository.get_history(conversation.id)

    assert history == [tool_message]
    assert history[0].tool_call_id == "call-1"
    assert history[0].is_error is True


@pytest.mark.integration
async def test_get_history_orders_by_sequence(db_session: AsyncSession) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    for index in range(5):
        await repository.append_message(
            conversation.id, ChatMessage(role="user", content=str(index))
        )

    history = await repository.get_history(conversation.id)

    assert [message.content for message in history] == ["0", "1", "2", "3", "4"]


@pytest.mark.integration
async def test_get_history_limit_returns_most_recent_messages_in_order(
    db_session: AsyncSession,
) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    for index in range(5):
        await repository.append_message(
            conversation.id, ChatMessage(role="user", content=str(index))
        )

    history = await repository.get_history(conversation.id, limit=2)

    assert [message.content for message in history] == ["3", "4"]


@pytest.mark.integration
async def test_get_or_create_default_conversation_is_idempotent(
    db_session: AsyncSession,
) -> None:
    repository = ConversationRepository(db_session)

    first = await repository.get_or_create_default_conversation()
    second = await repository.get_or_create_default_conversation()

    assert first.id == second.id


@pytest.mark.integration
async def test_get_or_create_default_conversation_is_safe_under_concurrent_first_calls(
    db_engine: AsyncEngine,
) -> None:
    async def _get_or_create_via_own_connection() -> uuid.UUID:
        async with AsyncSession(db_engine, expire_on_commit=False) as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_or_create_default_conversation()
            await session.commit()
            return conversation.id

    conversation_ids: set[uuid.UUID] = set()
    try:
        results = await asyncio.gather(
            *(_get_or_create_via_own_connection() for _ in range(8)),
            return_exceptions=True,
        )

        errors = [result for result in results if isinstance(result, BaseException)]
        assert not errors, errors

        conversation_ids = {result for result in results if isinstance(result, uuid.UUID)}
        assert len(conversation_ids) == 1
    finally:
        if conversation_ids:
            async with db_engine.begin() as connection:
                await connection.execute(
                    delete(Conversation).where(Conversation.id == conversation_ids.pop())
                )


@pytest.mark.integration
async def test_append_and_get_history_round_trips_empty_tool_calls_as_empty_list(
    db_session: AsyncSession,
) -> None:
    repository = ConversationRepository(db_session)
    conversation = await repository.create_conversation()

    message = ChatMessage(role="assistant", content="hello")
    assert message.tool_calls == []

    await repository.append_message(conversation.id, message)
    history = await repository.get_history(conversation.id)

    assert history[0].tool_calls == []
