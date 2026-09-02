from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

from libs.llm.base import Role

MAX_MESSAGE_LENGTH = 8000


class CreateConversationResponse(BaseModel):
    conversation_id: uuid.UUID


class SendMessageRequest(BaseModel):
    content: str = Field(max_length=MAX_MESSAGE_LENGTH)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content не должен быть пустым")
        return value


class MessageResponse(BaseModel):
    role: Role
    content: str | None = None
