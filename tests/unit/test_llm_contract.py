from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from libs.llm import ChatMessage, LLMResponse, ToolCall, ToolSpec

VALID_MESSAGES: list[tuple[str, dict[str, Any]]] = [
    ("system_with_content", {"role": "system", "content": "ты системный агент"}),
    ("user_with_content", {"role": "user", "content": "собери отчёт за август"}),
    ("assistant_text_only", {"role": "assistant", "content": "готово"}),
    (
        "assistant_tool_calls_only",
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [ToolCall(id="c1", name="write_document", arguments={"text": "hi"})],
        },
    ),
    (
        "assistant_text_and_tool_calls",
        {
            "role": "assistant",
            "content": "сейчас запишу",
            "tool_calls": [ToolCall(id="c1", name="write_document", arguments={})],
        },
    ),
    ("tool_result_ok", {"role": "tool", "content": "документ сохранён", "tool_call_id": "c1"}),
]

INVALID_MESSAGES: list[tuple[str, dict[str, Any]]] = [
    ("assistant_without_content_or_tool_calls", {"role": "assistant", "content": None}),
    (
        "tool_calls_on_non_assistant",
        {
            "role": "user",
            "content": "привет",
            "tool_calls": [ToolCall(id="c1", name="write_document", arguments={})],
        },
    ),
    ("tool_without_tool_call_id", {"role": "tool", "content": "документ сохранён"}),
    (
        "tool_without_content",
        {"role": "tool", "content": None, "tool_call_id": "c1"},
    ),
    (
        "tool_call_id_on_non_tool",
        {"role": "user", "content": "привет", "tool_call_id": "c1"},
    ),
    (
        "is_error_on_non_tool",
        {"role": "assistant", "content": "не вышло", "is_error": True},
    ),
    ("extra_field_forbidden", {"role": "user", "content": "привет", "name": "write_document"}),
]

VALID_RESPONSES: list[tuple[str, dict[str, Any]]] = [
    (
        "tool_use_with_tool_calls",
        {
            "model": "claude-sonnet-5",
            "stop_reason": "tool_use",
            "tool_calls": [ToolCall(id="c1", name="search", arguments={})],
        },
    ),
    (
        "end_turn_without_tool_calls",
        {"model": "claude-sonnet-5", "stop_reason": "end_turn", "text": "готово"},
    ),
    (
        "content_filter_without_tool_calls",
        {"model": "claude-sonnet-5", "stop_reason": "content_filter"},
    ),
    (
        "max_tokens_without_tool_calls",
        {"model": "claude-sonnet-5", "stop_reason": "max_tokens", "text": "часть отве"},
    ),
]

INVALID_RESPONSES: list[tuple[str, dict[str, Any]]] = [
    (
        "tool_use_without_tool_calls",
        {"model": "claude-sonnet-5", "stop_reason": "tool_use"},
    ),
    (
        "end_turn_with_tool_calls",
        {
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "text": "готово",
            "tool_calls": [ToolCall(id="c1", name="search", arguments={})],
        },
    ),
    (
        "content_filter_with_tool_calls",
        {
            "model": "claude-sonnet-5",
            "stop_reason": "content_filter",
            "tool_calls": [ToolCall(id="c1", name="search", arguments={})],
        },
    ),
]


@pytest.mark.parametrize(
    "kwargs",
    [kwargs for _, kwargs in VALID_MESSAGES],
    ids=[case for case, _ in VALID_MESSAGES],
)
def test_chat_message_accepts_valid_shape(kwargs: dict[str, Any]) -> None:
    message = ChatMessage(**kwargs)
    assert message.role == kwargs["role"]


@pytest.mark.parametrize(
    "kwargs",
    [kwargs for _, kwargs in INVALID_MESSAGES],
    ids=[case for case, _ in INVALID_MESSAGES],
)
def test_chat_message_rejects_invalid_shape(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChatMessage(**kwargs)


def test_tool_spec_from_model_builds_schema_from_pydantic() -> None:
    class WriteDocumentArgs(BaseModel):
        path: str
        text: str
        overwrite: bool = False

    spec = ToolSpec.from_model("write_document", "записать текст в документ", WriteDocumentArgs)

    assert spec.name == "write_document"
    assert spec.description == "записать текст в документ"
    assert spec.input_schema == WriteDocumentArgs.model_json_schema()
    assert spec.input_schema["type"] == "object"
    assert set(spec.input_schema["properties"]) == {"path", "text", "overwrite"}
    assert spec.input_schema["required"] == ["path", "text"]


def test_llm_response_to_message_preserves_tool_calls() -> None:
    tool_calls = [
        ToolCall(id="c1", name="write_document", arguments={"path": "report.docx"}),
        ToolCall(id="c2", name="search", arguments={"query": "август"}),
    ]
    response = LLMResponse(
        model="claude-sonnet-5",
        stop_reason="tool_use",
        text="сейчас всё сделаю",
        tool_calls=tool_calls,
    )

    message = response.to_message()

    assert response.has_tool_calls is True
    assert message.role == "assistant"
    assert message.content == "сейчас всё сделаю"
    assert message.tool_calls == tool_calls


def test_llm_response_to_message_drops_empty_text() -> None:
    response = LLMResponse(
        model="claude-sonnet-5",
        stop_reason="tool_use",
        tool_calls=[ToolCall(id="c1", name="search", arguments={})],
    )

    assert response.to_message().content is None


@pytest.mark.parametrize(
    "kwargs",
    [kwargs for _, kwargs in VALID_RESPONSES],
    ids=[case for case, _ in VALID_RESPONSES],
)
def test_llm_response_accepts_consistent_stop_reason(kwargs: dict[str, Any]) -> None:
    response = LLMResponse(**kwargs)
    assert response.has_tool_calls == (response.stop_reason == "tool_use")


@pytest.mark.parametrize(
    "kwargs",
    [kwargs for _, kwargs in INVALID_RESPONSES],
    ids=[case for case, _ in INVALID_RESPONSES],
)
def test_llm_response_rejects_inconsistent_stop_reason(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LLMResponse(**kwargs)


def test_llm_response_to_message_keeps_empty_text_without_tool_calls() -> None:
    response = LLMResponse(model="claude-sonnet-5", stop_reason="content_filter")

    message = response.to_message()

    assert response.has_tool_calls is False
    assert message.role == "assistant"
    assert message.content == ""


def test_assert_tool_calls_conform_to_contract_accepts_valid_calls(
    assert_tool_calls_conform_to_contract: Callable[[Sequence[ToolCall]], None],
) -> None:
    tool_calls = [
        ToolCall(id="c1", name="write_document", arguments={"path": "report.docx"}),
        ToolCall(id="c2", name="search", arguments={}),
    ]

    assert_tool_calls_conform_to_contract(tool_calls)


@pytest.mark.parametrize(
    "bad_call",
    [
        ToolCall.model_construct(id="", name="search", arguments={}),
        ToolCall.model_construct(id="c1", name="", arguments={}),
        ToolCall.model_construct(id="c1", name="search", arguments="not-a-dict"),  # type: ignore[arg-type]
    ],
)
def test_assert_tool_calls_conform_to_contract_rejects_broken_calls(
    bad_call: ToolCall,
    assert_tool_calls_conform_to_contract: Callable[[Sequence[ToolCall]], None],
) -> None:
    with pytest.raises(AssertionError):
        assert_tool_calls_conform_to_contract([bad_call])
