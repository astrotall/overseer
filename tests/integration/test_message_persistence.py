from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from libs.db.models import Conversation, Message


@pytest.mark.integration
async def test_messages_order_by_sequence_not_created_at(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    same_moment = datetime.now(UTC)
    first = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="calling a tool",
        created_at=same_moment,
    )
    second = Message(
        conversation_id=conversation.id,
        role="tool",
        content="result",
        tool_call_id="call-1",
        created_at=same_moment,
    )
    db_session.add(first)
    await db_session.flush()
    db_session.add(second)
    await db_session.flush()

    result = await db_session.execute(
        select(Conversation).where(Conversation.id == conversation.id)
    )
    loaded = result.scalar_one()

    assert [message.id for message in loaded.messages] == [first.id, second.id]
    assert first.sequence < second.sequence


@pytest.mark.integration
async def test_tool_role_requires_tool_call_id(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(Message(conversation_id=conversation.id, role="tool", content="result"))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_non_tool_role_forbids_tool_call_id(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="hi", tool_call_id="call-1")
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_non_tool_role_forbids_is_error_true(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="hi", is_error=True)
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_non_tool_role_allows_is_error_false(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    message = Message(conversation_id=conversation.id, role="user", content="hi", is_error=False)
    db_session.add(message)
    await db_session.flush()

    assert message.sequence is not None
    assert message.is_error is False


@pytest.mark.integration
async def test_tool_role_with_tool_call_id_is_valid(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    message = Message(
        conversation_id=conversation.id,
        role="tool",
        content="result",
        tool_call_id="call-1",
        is_error=True,
    )
    db_session.add(message)
    await db_session.flush()

    assert message.sequence is not None
    assert message.is_error is True


@pytest.mark.integration
async def test_tool_calls_none_is_stored_as_sql_null(db_session: AsyncSession) -> None:
    conversation = Conversation()
    db_session.add(conversation)
    await db_session.flush()

    message = Message(conversation_id=conversation.id, role="user", content="hi", tool_calls=None)
    db_session.add(message)
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT tool_calls IS NULL FROM messages WHERE id = :id"), {"id": message.id}
    )
    assert result.scalar_one() is True
