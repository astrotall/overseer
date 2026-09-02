from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from libs.db.models import Conversation, Message
from libs.llm.base import ChatMessage

_DEFAULT_CONVERSATION_ID = uuid.UUID(int=0)


def _to_message(conversation_id: uuid.UUID, chat_message: ChatMessage) -> Message:
    return Message(
        conversation_id=conversation_id,
        role=chat_message.role,
        content=chat_message.content,
        tool_calls=chat_message.tool_calls or None,
        tool_call_id=chat_message.tool_call_id,
        is_error=chat_message.is_error,
    )


def _to_chat_message(message: Message) -> ChatMessage:
    return ChatMessage(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls or [],
        tool_call_id=message.tool_call_id,
        is_error=message.is_error,
    )


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_conversation(self, *, title: str | None = None) -> uuid.UUID:
        conversation = Conversation(title=title)
        self._session.add(conversation)
        await self._session.flush()
        return conversation.id

    async def append_message(self, conversation_id: uuid.UUID, message: ChatMessage) -> None:
        db_message = _to_message(conversation_id, message)
        self._session.add(db_message)
        await self._session.flush()

    async def get_history(
        self, conversation_id: uuid.UUID, limit: int | None = None
    ) -> list[ChatMessage]:
        if limit is not None and limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")

        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self._session.execute(stmt)
        messages = result.scalars().all()
        return [_to_chat_message(message) for message in reversed(messages)]

    async def get_or_create_default_conversation(self) -> uuid.UUID:
        conversation = await self._session.get(Conversation, _DEFAULT_CONVERSATION_ID)
        if conversation is not None:
            return conversation.id

        stmt = (
            pg_insert(Conversation)
            .values(id=_DEFAULT_CONVERSATION_ID)
            .on_conflict_do_nothing(index_elements=[Conversation.id])
            .returning(Conversation.id)
        )
        result = await self._session.execute(stmt)
        conversation_id = result.scalar_one_or_none()
        if conversation_id is not None:
            return conversation_id

        conversation = await self._session.get(Conversation, _DEFAULT_CONVERSATION_ID)
        assert conversation is not None
        return conversation.id
