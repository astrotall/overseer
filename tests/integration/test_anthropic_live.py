from __future__ import annotations

import pytest

from libs.core.config import get_settings
from libs.llm.anthropic_client import AnthropicClient
from libs.llm.base import ChatMessage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not get_settings().anthropic_api_key,
        reason="ANTHROPIC_API_KEY not set",
    ),
]


async def test_complete_returns_plain_text_answer() -> None:
    client = AnthropicClient()
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
