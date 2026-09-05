from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.llm.base import ToolSpec

logger = get_logger(__name__)

ToolStatus = Literal["ok", "error"]

RUNTIME_FAILURES: tuple[type[Exception], ...] = (MemoryError, RecursionError, SystemError)

ArgumentsT = TypeVar("ArgumentsT", bound=BaseModel)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    @model_validator(mode="after")
    def _check_status_shape(self) -> Self:
        if not self.summary.strip():
            raise ValueError("summary обязателен: он уходит в логи, в аудит и в озвучку")

        if self.status == "error":
            if not self.error:
                raise ValueError("status='error' требует непустой error")
        elif self.error is not None:
            raise ValueError("error допустим только при status='error'")

        return self

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @classmethod
    def ok(cls, summary: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(status="ok", summary=summary, data=data or {})

    @classmethod
    def failed(cls, error: str, *, summary: str | None = None) -> ToolResult:
        return cls(status="error", summary=summary or error, error=error)


class Tool(ABC, Generic[ArgumentsT]):
    name: ClassVar[str]
    description: ClassVar[str]
    requires_confirmation: ClassVar[bool] = False

    arguments_model: type[ArgumentsT]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls._execute, "__isabstractmethod__", False):
            return
        missing = [
            attribute
            for attribute in ("name", "description", "arguments_model")
            if getattr(cls, attribute, None) is None
        ]
        if missing:
            raise ConfigurationError(
                f"Инструмент {cls.__name__} не объявил обязательные атрибуты: {', '.join(missing)}"
            )

    @property
    def parameters(self) -> dict[str, Any]:
        return self.arguments_model.model_json_schema()

    def to_spec(self) -> ToolSpec:
        return ToolSpec.from_model(self.name, self.description, self.arguments_model)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            parsed = self.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            logger.warning(
                "tool.invalid_arguments",
                tool=self.name,
                errors=[error["type"] for error in exc.errors()],
            )
            return ToolResult.failed(
                f"Инструмент {self.name} получил некорректные аргументы: "
                f"{_format_validation_error(exc)}"
            )

        try:
            return await self._execute(parsed)
        except RUNTIME_FAILURES:
            logger.exception("tool.execution_crashed", tool=self.name)
            raise
        except Exception as exc:
            logger.exception("tool.execution_failed", tool=self.name)
            return ToolResult.failed(f"Инструмент {self.name} завершился ошибкой: {exc}")

    @abstractmethod
    async def _execute(self, arguments: ArgumentsT) -> ToolResult: ...


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(item) for item in error["loc"]) or "<аргументы>"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
