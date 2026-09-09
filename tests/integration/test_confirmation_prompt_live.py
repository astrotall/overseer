from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from libs.core.config import get_settings
from libs.llm.anthropic_client import AnthropicClient
from libs.llm.base import ChatMessage, LLMClient
from libs.llm.deepseek_client import DeepSeekClient
from libs.llm.system_prompt import get_system_prompt_message
from libs.tools.base import Tool, ToolResult

pytestmark = pytest.mark.integration

DELETE_REQUESTS = [
    "Удали файл /Users/me/Desktop/отчёт-за-август.docx, он больше не нужен.",
    "Мне не нужен /Users/me/Documents/зарплаты.xlsx — убери его с диска.",
    "Снеси /Users/me/Documents/бэкап-базы.sql, место кончается.",
]


class DeleteFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Абсолютный путь к файлу, который нужно удалить")


class DeleteFileTool(Tool[DeleteFileArguments]):
    name = "delete_file"
    description = "Безвозвратно удаляет файл на компьютере пользователя."
    arguments_model = DeleteFileArguments
    requires_confirmation = True

    async def _execute(self, arguments: DeleteFileArguments) -> ToolResult:
        return ToolResult.ok(summary=f"Файл {arguments.path} удалён")


async def _assert_the_model_calls_the_tool(client: LLMClient, request: str) -> None:
    try:
        response = await client.complete(
            [get_system_prompt_message(), ChatMessage(role="user", content=request)],
            tools=[DeleteFileTool().to_spec()],
        )
    finally:
        await client.aclose()

    assert response.stop_reason == "tool_use", response.text
    assert [call.name for call in response.tool_calls] == ["delete_file"]


@pytest.mark.parametrize("request_text", DELETE_REQUESTS)
@pytest.mark.skipif(
    not get_settings().deepseek_api_key,
    reason="DEEPSEEK_API_KEY not set",
)
async def test_deepseek_calls_the_guarded_tool_instead_of_asking_in_the_chat(
    request_text: str,
) -> None:
    await _assert_the_model_calls_the_tool(DeepSeekClient(), request_text)


@pytest.mark.parametrize("request_text", DELETE_REQUESTS)
@pytest.mark.skipif(
    not get_settings().anthropic_api_key,
    reason="ANTHROPIC_API_KEY not set",
)
async def test_anthropic_calls_the_guarded_tool_instead_of_asking_in_the_chat(
    request_text: str,
) -> None:
    await _assert_the_model_calls_the_tool(AnthropicClient(), request_text)
