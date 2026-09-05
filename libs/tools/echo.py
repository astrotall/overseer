from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from libs.tools.base import Tool, ToolResult


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description="Текст, который вернётся без изменений")


class EchoTool(Tool[EchoArguments]):
    name = "echo"
    description = (
        "Возвращает переданный текст без изменений. "
        "Служебный инструмент для проверки цикла вызова, полезной работы не делает."
    )
    arguments_model = EchoArguments

    async def _execute(self, arguments: EchoArguments) -> ToolResult:
        return ToolResult.ok(summary=arguments.text, data={"text": arguments.text})
