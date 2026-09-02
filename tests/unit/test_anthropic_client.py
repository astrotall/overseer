from __future__ import annotations

import json

import httpx2
import pytest

from libs.core.config import Settings
from libs.core.exceptions import LLMBadRequestError, LLMResponseError, LLMTransientError
from libs.llm.anthropic_client import AnthropicClient
from libs.llm.base import ChatMessage, ToolCall, ToolSpec


def _settings() -> Settings:
    return Settings(anthropic_api_key="test-key", anthropic_model="claude-sonnet-4-5")


def _client(handler: httpx2.MockTransport, *, max_retries: int = 0) -> AnthropicClient:
    http_client = httpx2.AsyncClient(transport=handler)
    return AnthropicClient(_settings(), max_retries=max_retries, http_client=http_client)


def _message_response(
    *,
    text: str = "здравствуйте",
    stop_reason: str = "end_turn",
    model: str = "claude-sonnet-4-5",
    input_tokens: int = 5,
    output_tokens: int = 2,
) -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


async def test_complete_maps_text_response() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-4-5"
        assert body["system"] == "ты помощник"
        assert body["messages"] == [{"role": "user", "content": "привет"}]
        return httpx2.Response(200, json=_message_response())

    client = _client(httpx2.MockTransport(handler))
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


async def test_complete_encodes_tool_call_history() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert "system" not in body
        assert body["messages"] == [
            {"role": "user", "content": "сделай документ"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "write_document",
                        "input": {"path": "a.docx"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "готово",
                        "is_error": False,
                    }
                ],
            },
        ]
        return httpx2.Response(200, json=_message_response(text="документ готов"))

    client = _client(httpx2.MockTransport(handler))
    result = await client.complete(
        [
            ChatMessage(role="user", content="сделай документ"),
            ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id="call_1", name="write_document", arguments={"path": "a.docx"})
                ],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", content="готово"),
        ]
    )

    assert result.text == "документ готов"
    await client.aclose()


async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad payload"},
            },
        )

    client = _client(httpx2.MockTransport(handler), max_retries=3)
    with pytest.raises(LLMBadRequestError, match="bad payload"):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 1
    await client.aclose()


async def test_retries_transient_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx2.Response(503, headers={"retry-after": "0.01"}, text="unavailable")
        return httpx2.Response(200, json=_message_response(text="ок"))

    client = _client(httpx2.MockTransport(handler), max_retries=5)
    result = await client.complete([ChatMessage(role="user", content="привет")])
    assert result.text == "ок"
    assert calls == 3
    await client.aclose()


async def test_exhausted_retries_raise_transient() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, headers={"retry-after": "0.01"}, text="boom")

    client = _client(httpx2.MockTransport(handler), max_retries=2)
    with pytest.raises(LLMTransientError):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 3
    await client.aclose()


async def test_response_missing_required_field_is_response_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = _message_response()
        del payload["usage"]
        return httpx2.Response(200, json=payload)

    client = _client(httpx2.MockTransport(handler))
    with pytest.raises(LLMResponseError):
        await client.complete([ChatMessage(role="user", content="привет")])
    await client.aclose()


async def test_missing_stop_reason_is_response_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = _message_response()
        payload["stop_reason"] = None
        return httpx2.Response(200, json=payload)

    client = _client(httpx2.MockTransport(handler))
    with pytest.raises(LLMResponseError, match="stop_reason"):
        await client.complete([ChatMessage(role="user", content="привет")])
    await client.aclose()


async def test_tool_use_stop_reason_is_response_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = _message_response(stop_reason="tool_use")
        return httpx2.Response(200, json=payload)

    client = _client(httpx2.MockTransport(handler))
    with pytest.raises(LLMResponseError, match="OVE-4"):
        await client.complete([ChatMessage(role="user", content="привет")])
    await client.aclose()


async def test_complete_with_tools_raises_not_implemented() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("запрос не должен уйти в API, если переданы tools")

    client = _client(httpx2.MockTransport(handler))
    tools = [ToolSpec(name="write_document", description="пишет документ")]
    with pytest.raises(LLMBadRequestError, match="OVE-4"):
        await client.complete([ChatMessage(role="user", content="привет")], tools=tools)
    await client.aclose()


@pytest.mark.parametrize("status_code", [408, 409])
async def test_exhausted_retries_on_408_409_raise_transient(status_code: int) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(status_code, headers={"retry-after": "0.01"}, text="boom")

    client = _client(httpx2.MockTransport(handler), max_retries=2)
    with pytest.raises(LLMTransientError):
        await client.complete([ChatMessage(role="user", content="привет")])
    assert calls == 3
    await client.aclose()
