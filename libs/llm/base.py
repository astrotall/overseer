from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]

StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "content_filter"]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False

    @model_validator(mode="after")
    def _check_role_shape(self) -> Self:
        if self.role == "assistant":
            if self.content is None and not self.tool_calls:
                raise ValueError("Сообщение assistant должно нести content или tool_calls")
        elif self.tool_calls:
            raise ValueError("tool_calls допустимы только у сообщения с role='assistant'")

        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("Сообщение tool должно нести tool_call_id вызова")
            if self.content is None:
                raise ValueError("Сообщение tool должно нести content — результат инструмента")
        else:
            if self.tool_call_id is not None:
                raise ValueError("tool_call_id допустим только у сообщения с role='tool'")
            if self.is_error:
                raise ValueError("is_error допустим только у сообщения с role='tool'")

        if self.role in ("system", "user") and self.content is None:
            raise ValueError(f"Сообщение {self.role} должно нести content")

        return self


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    @classmethod
    def from_model(cls, name: str, description: str, arguments: type[BaseModel]) -> ToolSpec:
        return cls(name=name, description=description, input_schema=arguments.model_json_schema())


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    stop_reason: StopReason
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage | None = None
    raw: dict[str, Any] | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=self.text or None,
            tool_calls=list(self.tool_calls),
        )


class LLMClient(ABC):
    default_model: ClassVar[str] = ""

    @abstractmethod
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse: ...

    async def aclose(self) -> None:
        return None
