from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NoReturn, cast

import httpx2
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    AsyncAnthropic,
)
from anthropic.types import Message, MessageParam
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
    ToolSpec,
    Usage,
)

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

_STOP_REASON: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "refusal": "content_filter",
    "model_context_window_exceeded": "max_tokens",
}


class AnthropicClient(LLMClient):
    default_model = "claude-sonnet-4-5"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        max_retries: int = 2,
        timeout: float = 60.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        settings = settings or get_settings()
        if not settings.anthropic_api_key:
            raise ConfigurationError("Не задан ANTHROPIC_API_KEY")
        self._model = settings.anthropic_model
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        if tools:
            raise LLMResponseError(
                "Anthropic-клиент пока не поддерживает передачу tools в запрос (OVE-4)"
            )
        system, encoded_messages = _encode_messages(messages)
        kwargs: dict[str, Any] = {}
        if system is not None:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature

        try:
            message = await self._client.messages.create(
                model=model or self._model,
                messages=cast(list[MessageParam], encoded_messages),
                max_tokens=max_tokens,
                **kwargs,
            )
        except APIResponseValidationError as exc:
            raise LLMResponseError(
                f"Anthropic вернул ответ неожиданной формы: {exc.message}"
            ) from exc
        except APIConnectionError as exc:
            raise LLMTransientError(f"Не удалось достучаться до Anthropic: {exc.message}") from exc
        except APIStatusError as exc:
            _raise_for_status(exc)
        except AnthropicError as exc:
            raise LLMError(f"Ошибка Anthropic SDK: {exc}") from exc

        return _decode_response(message)


def _encode_messages(messages: Sequence[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts = [
        message.content for message in messages if message.role == "system" and message.content
    ]
    system = "\n\n".join(system_parts) if system_parts else None

    encoded: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                    "is_error": message.is_error,
                }
            )
            continue
        if pending_tool_results:
            encoded.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []
        encoded.append(_encode_turn(message))

    if pending_tool_results:
        encoded.append({"role": "user", "content": pending_tool_results})

    return system, encoded


def _encode_turn(message: ChatMessage) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in message.tool_calls
        )
        return {"role": "assistant", "content": blocks}
    return {"role": message.role, "content": message.content}


def _decode_response(message: Message) -> LLMResponse:
    model = message.model
    if not isinstance(model, str) or not model:
        raise LLMResponseError("В ответе Anthropic нет model")

    usage = message.usage
    if usage is None:
        raise LLMResponseError("В ответе Anthropic нет usage")

    text = "".join(block.text for block in message.content if block.type == "text")
    stop_reason = _decode_stop_reason(message.stop_reason)

    try:
        return LLMResponse(
            model=model,
            stop_reason=stop_reason,
            text=text,
            usage=Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            raw=message.model_dump(mode="json"),
        )
    except ValidationError as exc:
        raise LLMResponseError(
            f"Не удалось собрать LLMResponse из ответа Anthropic: {exc}"
        ) from exc


def _decode_stop_reason(native: str | None) -> StopReason:
    if native is None:
        raise LLMResponseError("Anthropic не прислал stop_reason")
    mapped = _STOP_REASON.get(native)
    if mapped is None:
        raise LLMResponseError(f"Anthropic вернул неизвестный stop_reason: {native}")
    if mapped == "tool_use":
        raise LLMResponseError(
            "Anthropic запросил вызов инструмента, но разбор новых tool_use "
            "ещё не реализован (OVE-4)"
        )
    return mapped


def _raise_for_status(exc: APIStatusError) -> NoReturn:
    detail = exc.message[:500]
    if exc.status_code in _RETRYABLE_STATUS:
        raise LLMTransientError(f"Anthropic вернул {exc.status_code}: {detail}") from exc
    raise LLMBadRequestError(f"Anthropic отклонил запрос ({exc.status_code}): {detail}") from exc
