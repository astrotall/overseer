from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from libs.db.base import Base
from libs.db.models.conversation import Conversation
from libs.llm.base import Role, ToolCall


class ToolCallListType(TypeDecorator[list[ToolCall]]):
    """(Де)сериализует list[ToolCall] в jsonb через Pydantic, без ручного dict-маппинга."""

    impl = JSONB(none_as_null=True)
    cache_ok = True

    def process_bind_param(
        self, value: list[ToolCall] | None, dialect: Dialect
    ) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        return [tool_call.model_dump(mode="json") for tool_call in value]

    def process_result_value(self, value: Any, dialect: Dialect) -> list[ToolCall] | None:
        if value is None:
            return None
        return [ToolCall.model_validate(item) for item in value]


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "(role = 'tool' AND tool_call_id IS NOT NULL) "
            "OR (role <> 'tool' AND tool_call_id IS NULL)",
            name="tool_role_requires_tool_call_id",
        ),
        CheckConstraint(
            "role = 'tool' OR is_error = false",
            name="non_tool_role_forbids_is_error",
        ),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="role_is_valid",
        ),
        Index("ix_messages_conversation_id_sequence", "conversation_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    role: Mapped[Role] = mapped_column(String, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list[ToolCall] | None] = mapped_column(ToolCallListType, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
