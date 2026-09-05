from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from structlog.typing import EventDict

from libs.core.exceptions import ConfigurationError
from libs.llm import ToolCall, ToolSpec
from libs.tools import EchoArguments, EchoTool, Tool, ToolResult


class BoomArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


SENSITIVE_FAILURE = (
    "[Errno 13] Permission denied: "
    r"'C:\Users\ivan\Документы\Зарплаты за август.docx'"
)


class BoomTool(Tool[BoomArguments]):
    name = "boom"
    description = "Всегда падает: проверка того, что исключение не выходит наружу"
    requires_confirmation = True
    arguments_model = BoomArguments

    async def _execute(self, arguments: BoomArguments) -> ToolResult:
        raise RuntimeError(SENSITIVE_FAILURE)


class CrashingTool(Tool[BoomArguments]):
    name = "crashing"
    description = "Падает так, как другие аргументы не чинят"
    arguments_model = BoomArguments

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    async def _execute(self, arguments: BoomArguments) -> ToolResult:
        raise self._failure


@contextmanager
def captured_logs() -> Iterator[list[EventDict]]:
    with structlog.testing.capture_logs([structlog.processors.format_exc_info]) as entries:
        yield entries


async def test_echo_returns_the_text_it_received() -> None:
    result = await EchoTool().execute({"text": "собери отчёт за август"})

    assert result == ToolResult(
        status="ok",
        summary="собери отчёт за август",
        data={"text": "собери отчёт за август"},
    )
    assert not result.is_error


async def test_a_tool_call_from_the_llm_is_executed_without_unpacking_arguments() -> None:
    call = ToolCall(id="call_1", name="echo", arguments={"text": "привет"})

    result = await EchoTool().execute(call.arguments)

    assert result.status == "ok"
    assert result.data == {"text": "привет"}


def test_the_spec_of_a_tool_is_built_from_its_arguments_model() -> None:
    tool = EchoTool()

    spec = tool.to_spec()

    assert spec == ToolSpec.from_model("echo", tool.description, EchoArguments)
    assert spec.input_schema == tool.parameters
    assert spec.input_schema["required"] == ["text"]


def test_confirmation_is_off_unless_the_tool_asks_for_it() -> None:
    assert EchoTool.requires_confirmation is False
    assert BoomTool.requires_confirmation is True


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("missing_field", {}),
        ("wrong_type", {"text": 42}),
        ("empty_text", {"text": ""}),
        ("unknown_field", {"text": "привет", "colour": "red"}),
    ],
)
async def test_arguments_the_model_got_wrong_come_back_as_an_error_result(
    name: str, arguments: dict[str, Any]
) -> None:
    result = await EchoTool().execute(arguments)

    assert result.is_error
    assert result.error
    assert "echo" in result.error
    assert result.summary == result.error


async def test_an_exception_inside_a_tool_comes_back_as_an_error_result() -> None:
    result = await BoomTool().execute({"value": 1})

    assert result.is_error
    assert result.error is not None
    assert "boom" in result.error
    assert result.summary == result.error


async def test_what_the_exception_said_never_reaches_the_model() -> None:
    result = await BoomTool().execute({"value": 1})

    assert result.error is not None
    assert SENSITIVE_FAILURE not in result.error
    assert "Зарплаты за август" not in repr(result)
    assert "ivan" not in repr(result)
    assert "RuntimeError" not in repr(result)


async def test_the_message_is_the_same_no_matter_what_went_wrong() -> None:
    one = await CrashingTool(OSError(SENSITIVE_FAILURE)).execute({"value": 1})
    another = await CrashingTool(TimeoutError("word.exe не ответил за 30 с")).execute({"value": 1})

    assert one == another


async def test_a_bug_inside_a_tool_is_logged_with_a_full_traceback() -> None:
    with captured_logs() as entries:
        result = await BoomTool().execute({"value": 1})

    (entry,) = entries
    assert entry["event"] == "tool.execution_failed"
    assert entry["log_level"] == "error"
    assert entry["tool"] == "boom"
    assert "Traceback (most recent call last)" in entry["exception"]
    assert f"RuntimeError: {SENSITIVE_FAILURE}" in entry["exception"]
    assert "_execute" in entry["exception"]
    assert result.error is not None
    assert SENSITIVE_FAILURE not in result.error


async def test_arguments_the_model_got_wrong_are_logged_without_a_traceback() -> None:
    with captured_logs() as entries:
        await EchoTool().execute({"text": "привет", "note": "пароль от почты — hunter2"})

    (entry,) = entries
    assert entry["event"] == "tool.invalid_arguments"
    assert entry["log_level"] == "warning"
    assert entry["tool"] == "echo"
    assert entry["errors"] == ["extra_forbidden"]
    assert "exception" not in entry
    assert "exc_info" not in entry
    assert "hunter2" not in repr(entry)


@pytest.mark.parametrize(
    ("name", "failure"),
    [
        ("out_of_memory", MemoryError("не хватило памяти на буфер документа")),
        ("too_deep", RecursionError("maximum recursion depth exceeded")),
        ("interpreter", SystemError("внутренняя ошибка интерпретатора")),
    ],
)
async def test_a_broken_runtime_is_not_disguised_as_a_tool_error(
    name: str, failure: Exception
) -> None:
    with captured_logs() as entries, pytest.raises(type(failure)):
        await CrashingTool(failure).execute({"value": 1})

    (entry,) = entries
    assert entry["event"] == "tool.execution_crashed"
    assert entry["log_level"] == "error"
    assert entry["tool"] == "crashing"
    assert "Traceback (most recent call last)" in entry["exception"]


async def test_stopping_the_process_is_not_a_tool_error_either() -> None:
    with captured_logs() as entries, pytest.raises(asyncio.CancelledError):
        await CrashingTool(asyncio.CancelledError()).execute({"value": 1})

    assert entries == []


async def test_a_tool_never_sees_arguments_it_did_not_declare() -> None:
    seen: list[EchoArguments] = []

    class SpyTool(EchoTool):
        name = "spy"

        async def _execute(self, arguments: EchoArguments) -> ToolResult:
            seen.append(arguments)
            return await super()._execute(arguments)

    await SpyTool().execute({"text": "привет"})
    await SpyTool().execute({"text": 1})

    assert seen == [EchoArguments(text="привет")]


def test_a_tool_that_forgot_to_declare_its_contract_does_not_import() -> None:
    with pytest.raises(ConfigurationError, match="arguments_model"):

        class Nameless(Tool[EchoArguments]):
            name = "nameless"
            description = "не объявил модель аргументов"

            async def _execute(self, arguments: EchoArguments) -> ToolResult:
                return ToolResult.ok(summary=arguments.text)


def test_an_abstract_tool_may_leave_the_contract_to_its_subclasses() -> None:
    class HalfTool(Tool[EchoArguments]):
        pass

    assert HalfTool.requires_confirmation is False


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("error_without_text", {"status": "error", "summary": "не вышло"}),
        ("error_text_on_success", {"status": "ok", "summary": "готово", "error": "не вышло"}),
        ("blank_summary", {"status": "ok", "summary": "   "}),
        ("unknown_status", {"status": "pending", "summary": "готово"}),
        ("extra_field_forbidden", {"status": "ok", "summary": "готово", "kind": "word"}),
    ],
)
def test_a_tool_result_of_the_wrong_shape_cannot_be_built(
    name: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        ToolResult(**payload)


def test_a_failure_speaks_with_its_error_text_unless_given_a_summary() -> None:
    assert ToolResult.failed("файл занят другим процессом").summary == (
        "файл занят другим процессом"
    )
    assert ToolResult.failed("WinError 32", summary="Не смог сохранить документ").summary == (
        "Не смог сохранить документ"
    )
