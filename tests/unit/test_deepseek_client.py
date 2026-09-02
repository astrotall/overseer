from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import httpx
import pytest

from libs.core.config import Settings
from libs.core.exceptions import LLMBadRequestError, LLMResponseError, LLMTransientError
from libs.llm.base import ChatMessage, ToolCall, ToolSpec
from libs.llm.deepseek_client import DeepSeekClient


def _settings() -> Settings:
    return Settings(deepseek_api_key="test-key", deepseek_model="deepseek-chat")


def _client(handler: httpx.MockTransport, **kwargs: object) -> DeepSeekClient:
    return DeepSeekClient(_settings(), backoff_base=0.0, transport=handler, **kwargs)  # type: ignore[arg-type]


async def test_complete_maps_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "deepseek-chat"
        assert body["messages"] == [
            {"role": "system", "content": "ты помощник"},
            {"role": "user", "content": "привет"},
        ]
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "здравствуйте"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.complete(
        [
            ChatMessage(role="system", content="ты помощник"),
            ChatMessage(role="user", content="привет"),
        ]
    )

    assert result.stop_reason == "end_turn"
    assert result.text == "здравствуйте"
    assert result.has_tool_calls is False
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (5, 2)
    assert result.raw is not None
    await client.aclose()


async def test_complete_maps_tool_calls(
    assert_tool_calls_conform_to_contract: Callable[[Sequence[ToolCall]], None],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "write_document"
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_document",
                                        "arguments": '{"path": "a.docx"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.complete(
        [ChatMessage(role="user", content="сделай документ")],
        tools=[ToolSpec(name="write_document", description="пишет документ")],
    )

    assert result.stop_reason == "tool_use"
    assert result.has_tool_calls is True
    assert_tool_calls_conform_to_contract(result.tool_calls)
    call = result.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "write_document", {"path": "a.docx"})
    assert result.to_message().role == "assistant"
    await client.aclose()


async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad payload"}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMBadRequestError, match="bad payload"):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 1
    await client.aclose()


async def test_retries_transient_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ок"}}
                ],
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.complete([ChatMessage(role="user", content="привет")])
    assert result.text == "ок"
    assert calls == 3
    await client.aclose()


async def test_exhausted_retries_raise_transient() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    client = _client(httpx.MockTransport(handler), max_retries=2)
    with pytest.raises(LLMTransientError):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 3
    await client.aclose()


async def test_retries_insufficient_system_resource_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "finish_reason": "insufficient_system_resource",
                            "message": {"role": "assistant", "content": None},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ок"}}
                ],
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.complete([ChatMessage(role="user", content="привет")])
    assert result.text == "ок"
    assert calls == 3
    await client.aclose()


async def test_exhausted_insufficient_system_resource_raise_transient() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "insufficient_system_resource",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
            },
        )

    client = _client(httpx.MockTransport(handler), max_retries=2)
    with pytest.raises(LLMTransientError, match="не хватило ресурсов"):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 3
    await client.aclose()


async def test_tool_calls_finish_reason_without_calls_is_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LLMResponseError):
        await client.complete([ChatMessage(role="user", content="привет")])
    await client.aclose()
