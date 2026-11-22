from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from pydantic import BaseModel, ConfigDict

from libs.core.exceptions import OfficeFileLockedError, OfficeTimeoutError
from libs.executor import (
    DEFAULT_CALL_BUDGET_S,
    FAILURE_TEXTS,
    UNKNOWN_OUTCOME_TEXT,
    ExecutorClient,
    FakeExecutorClient,
    failure_result,
)
from libs.executor.client import NOT_EXECUTED_SUMMARY, UNKNOWN_OUTCOME_SUMMARY
from libs.schemas.executor import (
    ERROR_OUTCOMES,
    MAX_CELL_TEXT_CHARS,
    MAX_REPLY_BYTES,
    MAX_REQUEST_BYTES,
    ExecutorError,
    ExecutorErrorCode,
    ExecutorRequest,
    ExecutorResponse,
    RangeWritten,
    SaveWorkbookArguments,
    SaveWorkbookRequest,
    WriteRangeArguments,
    WriteRangeRequest,
)
from libs.tools import Tool, ToolResult

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _clock() -> datetime:
    return NOW


def _save(*, deadline_s: float = DEFAULT_CALL_BUDGET_S) -> SaveWorkbookRequest:
    return SaveWorkbookRequest(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        issued_at=NOW - timedelta(seconds=1),
        deadline=NOW + timedelta(seconds=deadline_s),
        arguments=SaveWorkbookArguments(path="C:\\Отчёты\\август.xlsx"),
    )


def _write(data: list[list[str | int | float | bool | None]]) -> WriteRangeRequest:
    return WriteRangeRequest(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        issued_at=NOW,
        deadline=NOW + timedelta(seconds=DEFAULT_CALL_BUDGET_S),
        arguments=WriteRangeArguments(sheet="Лист1", start_cell="A1", data=data),
    )


class TestFake:
    async def test_the_fake_satisfies_the_port(self) -> None:
        client: ExecutorClient = FakeExecutorClient(clock=_clock)
        request = _save()

        response = await client.call(request)

        assert response == ExecutorResponse.ok(request.id)

    async def test_the_handler_sees_the_request_as_it_came_off_the_wire(self) -> None:
        seen: list[ExecutorRequest] = []

        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            seen.append(request)
            return ExecutorResponse.ok(request.id, {"path": "C:\\a.xlsx", "saved_as": False})

        client = FakeExecutorClient(handler, clock=_clock)
        request = _save()

        response = await client.call(request)

        assert seen == [request]
        assert seen[0] is not request
        assert client.requests == [request]
        assert response.data == {"path": "C:\\a.xlsx", "saved_as": False}

    async def test_a_stopped_executor_fails_fast_without_reaching_the_handler(self) -> None:
        client = FakeExecutorClient(clock=_clock, running=False)
        request = _save()

        response = await asyncio.wait_for(client.call(request), timeout=1)

        assert response.error == ExecutorError(code="executor_unavailable")
        assert response.error.outcome == "not_executed"
        assert client.requests == []

    @pytest.mark.parametrize("code", ["not_picked_up", "reply_timeout", "transport_error"])
    async def test_a_transport_failure_can_be_scripted(self, code: ExecutorErrorCode) -> None:
        client = FakeExecutorClient(clock=_clock)
        client.failure = code

        response = await client.call(_save())

        assert response.error == ExecutorError(code=code)
        assert client.requests == []

    async def test_a_duplicate_request_id_is_never_executed_twice(self) -> None:
        executed: list[uuid.UUID] = []

        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            executed.append(request.id)
            return ExecutorResponse.ok(request.id)

        client = FakeExecutorClient(handler, clock=_clock)
        request = _save()

        first = await client.call(request)
        second = await client.call(request)

        assert first.status == "ok"
        assert second.error == ExecutorError(code="reply_timeout")
        assert second.error.outcome == "unknown"
        assert executed == [request.id]

    async def test_an_expired_request_is_dropped_before_the_handler(self) -> None:
        client = FakeExecutorClient(clock=lambda: NOW + timedelta(seconds=10))

        response = await client.call(_save(deadline_s=5))

        assert response.error == ExecutorError(code="deadline_exceeded")
        assert client.requests == []

    async def test_an_oversized_request_never_leaves_the_sender(self) -> None:
        count = MAX_REQUEST_BYTES // MAX_CELL_TEXT_CHARS
        request = _write([["я" * MAX_CELL_TEXT_CHARS] for _ in range(count)])
        client = FakeExecutorClient(clock=_clock)

        response = await client.call(request)

        assert response.error == ExecutorError(code="payload_too_large")
        assert client.requests == []

    async def test_an_office_exception_in_the_handler_becomes_its_typed_code(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            raise OfficeFileLockedError("'C:\\Users\\ivan\\Зарплаты.xlsx' is read-only")

        client = FakeExecutorClient(handler, clock=_clock)

        response = await client.call(_save())

        assert response.error == ExecutorError(code="file_locked")
        assert "ivan" not in response.model_dump_json()

    async def test_an_office_timeout_is_an_unknown_outcome(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            raise OfficeTimeoutError()

        response = await FakeExecutorClient(handler, clock=_clock).call(_save())

        assert response.error == ExecutorError(code="timeout")
        assert response.error.outcome == "unknown"

    async def test_an_unexpected_exception_is_an_internal_error_without_its_text(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            raise RuntimeError("C:\\Users\\ivan\\secret.xlsx")

        response = await FakeExecutorClient(handler, clock=_clock).call(_save())

        assert response.error == ExecutorError(code="internal_error")
        assert "secret" not in response.model_dump_json()

    async def test_cancellation_is_not_swallowed(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await FakeExecutorClient(handler, clock=_clock).call(_save())

    async def test_a_reply_for_another_request_is_never_delivered(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            return ExecutorResponse.ok(uuid.uuid4())

        request = _save()
        response = await FakeExecutorClient(handler, clock=_clock).call(request)

        assert response.id == request.id
        assert response.error == ExecutorError(code="reply_timeout")

    async def test_an_oversized_reply_is_a_malformed_reply(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            return ExecutorResponse.ok(request.id, {"blob": "x" * MAX_REPLY_BYTES})

        response = await FakeExecutorClient(handler, clock=_clock).call(_save())

        assert response.error == ExecutorError(code="malformed_reply")
        assert response.error.outcome == "unknown"


class TestFailureResult:
    def test_every_code_has_a_fixed_text_for_the_model(self) -> None:
        assert set(FAILURE_TEXTS) == set(get_args(ExecutorErrorCode))
        assert all(text.strip() and "{" not in text for text in FAILURE_TEXTS.values())

    def test_every_unknown_outcome_tells_the_model_not_to_retry(self) -> None:
        for code, outcome in ERROR_OUTCOMES.items():
            if outcome == "unknown":
                assert FAILURE_TEXTS[code] == UNKNOWN_OUTCOME_TEXT, code
            else:
                assert FAILURE_TEXTS[code] != UNKNOWN_OUTCOME_TEXT, code
                assert "не выполнено" in FAILURE_TEXTS[code], code

    @pytest.mark.parametrize(
        ("code", "summary"),
        [("busy", NOT_EXECUTED_SUMMARY), ("timeout", UNKNOWN_OUTCOME_SUMMARY)],
    )
    def test_the_summary_says_only_the_outcome(self, code: ExecutorErrorCode, summary: str) -> None:
        result = failure_result(ExecutorError(code=code))

        assert result.is_error
        assert result.summary == summary
        assert result.error == FAILURE_TEXTS[code]


class _WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    start_cell: str
    data: list[list[str | int | float | bool | None]]


class RemoteWriteTool(Tool[_WriteArguments]):
    name = "excel_write_range"
    description = "Записывает диапазон в открытую книгу Excel на компьютере пользователя."
    arguments_model = _WriteArguments

    def __init__(self, client: ExecutorClient) -> None:
        self._client = client

    async def _execute(
        self, arguments: _WriteArguments, *, conversation_id: uuid.UUID
    ) -> ToolResult:
        request = WriteRangeRequest(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            issued_at=NOW,
            deadline=NOW + timedelta(seconds=DEFAULT_CALL_BUDGET_S),
            arguments=WriteRangeArguments(
                sheet=arguments.sheet, start_cell=arguments.start_cell, data=arguments.data
            ),
        )
        response = await self._client.call(request)
        if response.error is not None:
            return failure_result(response.error)
        written = RangeWritten.model_validate(response.data)
        return ToolResult.ok(f"Записано: {written.sheet}!{written.address}")


class TestRemoteToolOverThePort:
    async def test_a_remote_tool_is_an_ordinary_tool_over_the_port(self) -> None:
        async def handler(request: ExecutorRequest) -> ExecutorResponse:
            assert isinstance(request, WriteRangeRequest)
            written = RangeWritten(
                sheet=request.arguments.sheet,
                address="A1:B1",
                rows=1,
                columns=2,
                sheet_created=False,
            )
            return ExecutorResponse.ok(request.id, written.model_dump(mode="json"))

        client = FakeExecutorClient(handler, clock=_clock)
        conversation_id = uuid.uuid4()
        tool = RemoteWriteTool(client)

        result = await tool.execute(
            {"sheet": "Лист1", "start_cell": "A1", "data": [[1, "x"]]},
            conversation_id=conversation_id,
        )

        assert result == ToolResult.ok("Записано: Лист1!A1:B1")
        assert client.requests[0].conversation_id == conversation_id

    async def test_an_absent_executor_reaches_the_model_as_a_fixed_message(self) -> None:
        tool = RemoteWriteTool(FakeExecutorClient(clock=_clock, running=False))

        result = await tool.execute(
            {"sheet": "Лист1", "start_cell": "A1", "data": [[1]]},
            conversation_id=uuid.uuid4(),
        )

        assert result.error == FAILURE_TEXTS["executor_unavailable"]
        assert result.summary == NOT_EXECUTED_SUMMARY
