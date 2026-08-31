from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, NoReturn

import httpx
from pydantic import ValidationError

from libs.core.config import Settings, get_settings
from libs.core.exceptions import (
    ConfigurationError,
    LLMBadRequestError,
    LLMError,
    LLMResponseError,
    LLMTransientError,
)
from libs.llm.base import (
    ChatMessage,
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_FINISH_REASON: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filter",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


class DeepSeekClient(LLMClient):
    default_model = "deepseek-chat"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = settings or get_settings()
        if not settings.deepseek_api_key:
            raise ConfigurationError("Не задан DEEPSEEK_API_KEY")
        self._model = settings.deepseek_model or self.default_model
        self._url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": [_encode_message(message) for message in messages],
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = [_encode_tool(tool) for tool in tools]
        if temperature is not None:
            body["temperature"] = temperature

        payload = await self._post(body)
        return _decode_response(payload)

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            last_attempt = attempt == self._max_retries
            try:
                response = await self._client.post(self._url, json=body)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise LLMTransientError(f"Не удалось достучаться до DeepSeek: {exc}") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue
            except httpx.HTTPError as exc:
                raise LLMError(f"Некорректный запрос к DeepSeek: {exc}") from exc

            if response.is_success:
                return _load_json(response)

            if response.status_code in _RETRYABLE_STATUS and not last_attempt:
                await asyncio.sleep(self._backoff(attempt, response))
                continue

            _raise_for_status(response)

        raise LLMTransientError(f"DeepSeek недоступен после {self._max_retries + 1} попыток")

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                return float(retry_after)
        return self._backoff_base * (2**attempt)


def _encode_message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.content}


def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _load_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data: Any = response.json()
    except ValueError as exc:
        raise LLMResponseError(f"DeepSeek вернул тело не в формате JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("DeepSeek вернул JSON неожиданной формы (ожидался объект)")
    return data


def _decode_response(data: dict[str, Any]) -> LLMResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("В ответе DeepSeek нет choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise LLMResponseError("choice в ответе DeepSeek неожиданной формы")

    message = choice.get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("В choice ответа DeepSeek нет message")

    finish_reason = choice.get("finish_reason")
    if finish_reason == "insufficient_system_resource":
        raise LLMTransientError("DeepSeek: не хватило ресурсов провайдера, повторите запрос")

    tool_calls = _decode_tool_calls(message.get("tool_calls"))
    if finish_reason in ("tool_calls", "function_call") and not tool_calls:
        raise LLMResponseError("DeepSeek сообщил finish_reason=tool_calls, но вызовов не прислал")

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise LLMResponseError("content в ответе DeepSeek не строка")

    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise LLMResponseError("В ответе DeepSeek нет model")

    try:
        return LLMResponse(
            model=model,
            stop_reason=_stop_reason(finish_reason, has_tool_calls=bool(tool_calls)),
            text=content or "",
            tool_calls=tool_calls,
            usage=_decode_usage(data.get("usage")),
            raw=data,
        )
    except ValidationError as exc:
        raise LLMResponseError(f"Не удалось собрать LLMResponse из ответа DeepSeek: {exc}") from exc


def _decode_tool_calls(raw: Any) -> list[ToolCall]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LLMResponseError("tool_calls в ответе DeepSeek не список")

    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            raise LLMResponseError("элемент tool_calls в ответе DeepSeek неожиданной формы")
        function = item.get("function")
        if not isinstance(function, dict):
            raise LLMResponseError("в элементе tool_calls нет function")
        call_id = item.get("id")
        name = function.get("name")
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise LLMResponseError("в элементе tool_calls нет id или name")
        raw_arguments = function.get("arguments") or "{}"
        if not isinstance(raw_arguments, str):
            raise LLMResponseError("arguments в tool_call не строка JSON")
        try:
            arguments: Any = json.loads(raw_arguments)
        except ValueError as exc:
            raise LLMResponseError(f"не удалось разобрать arguments вызова {name}: {exc}") from exc
        if not isinstance(arguments, dict):
            raise LLMResponseError(f"arguments вызова {name} — не JSON-объект")
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return calls


def _decode_usage(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    prompt_tokens = raw.get("prompt_tokens", 0)
    completion_tokens = raw.get("completion_tokens", 0)
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    return Usage(input_tokens=prompt_tokens, output_tokens=completion_tokens)


def _stop_reason(finish_reason: Any, *, has_tool_calls: bool) -> StopReason:
    if has_tool_calls:
        return "tool_use"
    if isinstance(finish_reason, str):
        mapped = _FINISH_REASON.get(finish_reason)
        if mapped is not None and mapped != "tool_use":
            return mapped
    return "end_turn"


def _error_detail(response: httpx.Response) -> str:
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message: str = error["message"]
            return message
    return str(payload)[:500]


def _raise_for_status(response: httpx.Response) -> NoReturn:
    detail = _error_detail(response)
    status = response.status_code
    if status in _RETRYABLE_STATUS:
        raise LLMTransientError(f"DeepSeek вернул {status}: {detail}")
    raise LLMBadRequestError(f"DeepSeek отклонил запрос ({status}): {detail}")
