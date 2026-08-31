from __future__ import annotations

import pytest

from libs.core.config import get_settings
from libs.llm.base import ChatMessage, ToolSpec
from libs.llm.deepseek_client import DeepSeekClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().deepseek_api_key,
        reason="DEEPSEEK_API_KEY not set",
    ),
]


async def test_complete_returns_plain_text_answer() -> None:
    client = DeepSeekClient()
    try:
        response = await client.complete(
            [
                ChatMessage(role="system", content="Отвечай одним словом."),
                ChatMessage(role="user", content="Столица Франции?"),
            ]
        )
    finally:
        await client.aclose()

    assert response.stop_reason == "end_turn"
    assert response.text != ""
    assert response.usage is not None


async def test_complete_calls_tool() -> None:
    client = DeepSeekClient()
    try:
        response = await client.complete(
            [ChatMessage(role="user", content="Какая погода в Париже? Вызови инструмент.")],
            tools=[
                ToolSpec(
                    name="get_weather",
                    description="Погода в городе",
                    input_schema={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                )
            ],
        )
    finally:
        await client.aclose()

    assert response.stop_reason == "tool_use"
    assert response.tool_calls != []
