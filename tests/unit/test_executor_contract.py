from __future__ import annotations

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from apps.executor.excel import controller as excel_controller
from apps.executor.excel import validation as excel_validation
from libs.core import exceptions
from libs.core.exceptions import (
    OfficeBusyError,
    OfficeComError,
    OfficeError,
    OfficeFileExistsError,
    OfficeFileLockedError,
    OfficeInputError,
    OfficeNotInstalledError,
    OfficeTimeoutError,
    OfficeUnsavedChangesError,
)
from libs.executor import DEFAULT_CALL_BUDGET_S
from libs.schemas.executor import (
    ERROR_OUTCOMES,
    MAX_CALL_WINDOW_S,
    MAX_CELL_TEXT_CHARS,
    MAX_CELLS,
    MAX_HEARTBEAT_BYTES,
    MAX_PATH_CHARS,
    MAX_REPLY_BYTES,
    MAX_REQUEST_BYTES,
    MAX_SHEET_CHARS,
    OPERATIONS,
    REQUEST_ADAPTER,
    SCHEMA_VERSION,
    ExecutorAccepted,
    ExecutorContractError,
    ExecutorError,
    ExecutorErrorCode,
    ExecutorHeartbeat,
    ExecutorRequest,
    ExecutorResponse,
    OpenOrCreateWorkbookRequest,
    RangeWritten,
    SaveWorkbookRequest,
    WorkbookOpened,
    WorkbookSaved,
    WriteRangeArguments,
    WriteRangeRequest,
    encode_heartbeat,
    encode_reply,
    encode_request,
    error_code_for,
    parse_heartbeat,
    parse_reply,
    parse_request,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _envelope(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "issued_at": NOW.isoformat(),
        "deadline": (NOW + timedelta(seconds=30)).isoformat(),
        "operation": "excel.write_range",
        "arguments": {"sheet": "Лист1", "start_cell": "A1", "data": [["x", 1, 2.5, True, None]]},
    }
    document.update(overrides)
    return document


def _arguments(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"sheet": "S", "start_cell": "A1", "data": [[1]]}
    arguments.update(overrides)
    return arguments


def _raw(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False)


def _write_request(data: list[list[Any]] | None = None) -> WriteRangeRequest:
    return WriteRangeRequest(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        issued_at=NOW,
        deadline=NOW + timedelta(seconds=30),
        arguments=WriteRangeArguments(
            sheet="Лист1", start_cell="A1", data=data if data is not None else [["x"]]
        ),
    )


def _contract_error(raw: str | bytes) -> ExecutorContractError:
    with pytest.raises(ExecutorContractError) as caught:
        parse_request(raw)
    return caught.value


class TestRequest:
    def test_a_request_survives_the_wire_unchanged(self) -> None:
        request = _write_request([["текст", 1, 2.5, True, None], ["=SUM(A1)", 0, -1.0, False, ""]])

        parsed = parse_request(encode_request(request))

        assert parsed == request
        assert isinstance(parsed, WriteRangeRequest)

    def test_cell_values_keep_their_json_types(self) -> None:
        parsed = parse_request(_raw(_envelope()))

        assert isinstance(parsed, WriteRangeRequest)
        assert [type(value) for value in parsed.arguments.data[0]] == [
            str,
            int,
            float,
            bool,
            type(None),
        ]

    @pytest.mark.parametrize(
        ("operation", "arguments", "model"),
        [
            ("excel.open_or_create_workbook", {}, OpenOrCreateWorkbookRequest),
            ("excel.open_or_create_workbook", {"path": "C:\\a.xlsx"}, OpenOrCreateWorkbookRequest),
            ("excel.save", {"path": None, "overwrite": True}, SaveWorkbookRequest),
            ("excel.write_range", _arguments(start_cell="B2"), WriteRangeRequest),
        ],
    )
    def test_the_operation_selects_the_arguments_model(
        self, operation: str, arguments: dict[str, Any], model: type[object]
    ) -> None:
        parsed = parse_request(_raw(_envelope(operation=operation, arguments=arguments)))

        assert isinstance(parsed, model)

    def test_the_allowlist_is_exactly_the_operations_of_the_union(self) -> None:
        variants = get_args(get_args(ExecutorRequest)[0])

        assert {variant.model_fields["operation"].default for variant in variants} == OPERATIONS

    def test_an_unknown_operation_is_refused_by_name(self) -> None:
        document = _envelope(operation="shell.run", arguments={"command": "calc.exe"})

        error = _contract_error(_raw(document))

        assert error.code == "unknown_operation"
        assert str(error.request_id) == document["id"]

    @pytest.mark.parametrize("operation", [None, 42, ["excel.save"], {"name": "excel.save"}])
    def test_an_operation_that_is_not_a_string_is_unknown(self, operation: object) -> None:
        assert _contract_error(_raw(_envelope(operation=operation))).code == "unknown_operation"

    @pytest.mark.parametrize(
        "document",
        [
            _envelope(surprise=True),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": [[1]], "macro": "x"}),
            _envelope(callable="os.system"),
        ],
    )
    def test_unknown_fields_are_refused_at_every_level(self, document: dict[str, Any]) -> None:
        error = _contract_error(_raw(document))

        assert error.code == "malformed_request"
        assert "extra_forbidden" in error.errors

    @pytest.mark.parametrize(
        "document",
        [
            _envelope(id="not-a-uuid"),
            _envelope(conversation_id=None),
            _envelope(issued_at="2026-11-22T12:00:00"),
            _envelope(deadline=NOW.isoformat()),
            _envelope(deadline=(NOW - timedelta(seconds=1)).isoformat()),
            _envelope(deadline=(NOW + timedelta(seconds=MAX_CALL_WINDOW_S + 1)).isoformat()),
            _envelope(arguments={"sheet": "", "start_cell": "A1", "data": [[1]]}),
            _envelope(arguments=_arguments(sheet="S" * (MAX_SHEET_CHARS + 1))),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": []}),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": [[]]}),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": "abc"}),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": [[{"a": 1}]]}),
            _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": [[[1]]]}),
            _envelope(arguments=_arguments(formulas="yes")),
            _envelope(operation="excel.save", arguments={"overwrite": 1}),
            _envelope(operation="excel.save", arguments={"path": ""}),
            _envelope(operation="excel.save", arguments={"path": "C:\\" + "a" * MAX_PATH_CHARS}),
            _envelope(operation="excel.open_or_create_workbook", arguments={"path": 42}),
        ],
    )
    def test_invalid_shapes_are_malformed_requests(self, document: dict[str, Any]) -> None:
        assert _contract_error(_raw(document)).code == "malformed_request"

    def test_a_timezone_naive_deadline_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            WriteRangeRequest(
                id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                issued_at=NOW.replace(tzinfo=None),
                deadline=(NOW + timedelta(seconds=5)).replace(tzinfo=None),
                arguments=WriteRangeArguments(sheet="S", start_cell="A1", data=[[1]]),
            )

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_never_reach_a_cell(self, raw: str) -> None:
        text = _raw(_envelope()).replace("2.5", raw)

        assert _contract_error(text).code == "malformed_request"

    def test_the_cell_cap_counts_every_row(self) -> None:
        rows = [[1] * 100 for _ in range(MAX_CELLS // 100)]
        WriteRangeArguments(sheet="S", start_cell="A1", data=rows)

        with pytest.raises(ValidationError, match="ячеек"):
            WriteRangeArguments(sheet="S", start_cell="A1", data=[*rows, [1]])

    def test_a_cell_longer_than_excel_allows_is_refused(self) -> None:
        WriteRangeArguments(sheet="S", start_cell="A1", data=[["x" * MAX_CELL_TEXT_CHARS]])

        with pytest.raises(ValidationError):
            WriteRangeArguments(
                sheet="S", start_cell="A1", data=[["x" * (MAX_CELL_TEXT_CHARS + 1)]]
            )

    def test_strings_are_not_coerced_into_numbers_or_flags(self) -> None:
        document = _envelope(arguments={"sheet": "S", "start_cell": "A1", "data": [["1", "true"]]})

        parsed = parse_request(_raw(document))

        assert isinstance(parsed, WriteRangeRequest)
        assert parsed.arguments.data == [["1", "true"]]


class TestVersion:
    @pytest.mark.parametrize("version", [0, 2, SCHEMA_VERSION + 1])
    def test_another_integer_version_is_unsupported(self, version: int) -> None:
        document = _envelope(schema_version=version)

        error = _contract_error(_raw(document))

        assert error.code == "unsupported_version"
        assert str(error.request_id) == document["id"]

    def test_an_unsupported_version_is_reported_before_the_shape_is_checked(self) -> None:
        document = _envelope(schema_version=2, operation="excel.something_new", extra=1)

        assert _contract_error(_raw(document)).code == "unsupported_version"

    @pytest.mark.parametrize("version", [None, "1", 1.0, True])
    def test_a_version_that_is_not_an_integer_is_malformed(self, version: object) -> None:
        assert _contract_error(_raw(_envelope(schema_version=version))).code == "malformed_request"

    def test_a_missing_version_is_malformed(self) -> None:
        document = _envelope()
        del document["schema_version"]

        assert _contract_error(_raw(document)).code == "malformed_request"

    def test_a_bool_is_not_accepted_as_the_version_by_the_models_either(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorAccepted.model_validate({"id": str(uuid.uuid4()), "schema_version": True})


class TestPayloadCaps:
    def test_an_oversized_request_is_refused_before_it_is_parsed(self) -> None:
        raw = '{"id": "' + "x" * MAX_REQUEST_BYTES + '"}'

        error = _contract_error(raw)

        assert error.code == "payload_too_large"
        assert error.request_id is None

    def test_the_cap_is_in_bytes_not_characters(self) -> None:
        raw = _raw(_envelope(padding="я" * (MAX_REQUEST_BYTES // 2)))
        assert len(raw) <= MAX_REQUEST_BYTES < len(raw.encode("utf-8"))

        assert _contract_error(raw).code == "payload_too_large"
        assert _contract_error(raw.encode("utf-8")).code == "payload_too_large"

    def test_the_sender_cannot_encode_a_request_over_the_cap(self) -> None:
        count = MAX_REQUEST_BYTES // MAX_CELL_TEXT_CHARS
        rows = [["я" * MAX_CELL_TEXT_CHARS] for _ in range(count)]
        request = _write_request(rows)

        with pytest.raises(ExecutorContractError) as caught:
            encode_request(request)

        assert caught.value.code == "payload_too_large"
        assert caught.value.request_id == request.id

    def test_the_executor_cannot_encode_a_reply_over_the_cap(self) -> None:
        response = ExecutorResponse.ok(uuid.uuid4(), {"blob": "x" * MAX_REPLY_BYTES})

        with pytest.raises(ExecutorContractError) as caught:
            encode_reply(response)

        assert caught.value.code == "payload_too_large"

    @pytest.mark.parametrize("raw", ["", "{", "[]", '"text"', "42", b"\xff\xfe"])
    def test_something_that_is_not_a_json_object_is_malformed(self, raw: str | bytes) -> None:
        assert _contract_error(raw).code == "malformed_request"

    def test_pathologically_deep_nesting_is_malformed_not_a_crash(self) -> None:
        depth = 100_000
        raw = "[" * depth + "]" * depth

        assert _contract_error(raw).code == "malformed_request"


class TestReply:
    def test_both_reply_kinds_survive_the_wire(self) -> None:
        request_id = uuid.uuid4()
        accepted = ExecutorAccepted(id=request_id)
        result = ExecutorResponse.ok(request_id, {"path": "C:\\a.xlsx", "saved_as": True})

        assert parse_reply(encode_reply(accepted)) == accepted
        assert parse_reply(encode_reply(result)) == result

    def test_an_error_carries_a_code_and_no_text(self) -> None:
        response = ExecutorResponse.failed(uuid.uuid4(), "file_locked")

        assert set(ExecutorError.model_fields) == {"code"}
        assert json.loads(encode_reply(response))["error"] == {"code": "file_locked"}

    def test_a_reply_smuggling_raw_com_text_is_malformed(self) -> None:
        document = json.loads(encode_reply(ExecutorResponse.failed(uuid.uuid4(), "com_error")))
        document["error"]["message"] = "'C:\\Users\\ivan\\Зарплаты.xlsx' is read-only"

        with pytest.raises(ExecutorContractError) as caught:
            parse_reply(json.dumps(document))

        assert caught.value.code == "malformed_reply"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"status": "error"},
            {"status": "ok", "error": ExecutorError(code="busy")},
            {"status": "error", "error": ExecutorError(code="busy"), "data": {"a": 1}},
        ],
    )
    def test_status_and_error_must_agree(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ExecutorResponse(id=uuid.uuid4(), **kwargs)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(schema_version=2),
            lambda d: d.update(type="progress"),
            lambda d: d.update(error={"code": "made_up"}, status="error"),
            lambda d: d.update(extra=True),
        ],
    )
    def test_any_reply_problem_is_a_malformed_reply(self, mutate: Any) -> None:
        document = json.loads(encode_reply(ExecutorResponse.ok(uuid.uuid4())))
        mutate(document)

        with pytest.raises(ExecutorContractError) as caught:
            parse_reply(json.dumps(document))

        assert caught.value.code == "malformed_reply"

    def test_an_oversized_reply_is_malformed(self) -> None:
        with pytest.raises(ExecutorContractError) as caught:
            parse_reply("x" * (MAX_REPLY_BYTES + 1))

        assert caught.value.code == "malformed_reply"

    def test_result_models_describe_what_the_controller_returns(self) -> None:
        assert set(WorkbookOpened.model_fields) == {"path", "created"}
        assert set(RangeWritten.model_fields) == {
            "sheet",
            "address",
            "rows",
            "columns",
            "sheet_created",
        }
        assert set(WorkbookSaved.model_fields) == {"path", "saved_as"}
        assert set(excel_controller.WriteResult.__dataclass_fields__) == set(
            RangeWritten.model_fields
        )
        assert set(excel_controller.WorkbookInfo.__dataclass_fields__) == set(
            WorkbookOpened.model_fields
        )
        assert set(excel_controller.SaveResult.__dataclass_fields__) == set(
            WorkbookSaved.model_fields
        )


class TestHeartbeat:
    def _heartbeat(self, **overrides: Any) -> dict[str, Any]:
        document = json.loads(
            encode_heartbeat(
                ExecutorHeartbeat(
                    instance_id=uuid.uuid4(),
                    started_at=NOW,
                    sent_at=NOW,
                    operations=sorted(OPERATIONS),
                )
            )
        )
        document.update(overrides)
        return document

    def test_a_heartbeat_survives_the_wire(self) -> None:
        document = self._heartbeat()

        assert parse_heartbeat(json.dumps(document)).operations == sorted(OPERATIONS)

    def test_a_newer_executor_is_a_version_mismatch_even_with_new_fields(self) -> None:
        document = self._heartbeat(schema_version=2, capabilities=["word"])

        with pytest.raises(ExecutorContractError) as caught:
            parse_heartbeat(json.dumps(document))

        assert caught.value.code == "unsupported_version"

    @pytest.mark.parametrize(
        "raw",
        ["", "garbage", "x" * (MAX_HEARTBEAT_BYTES + 1), json.dumps({"schema_version": True})],
    )
    def test_a_garbled_heartbeat_means_no_usable_executor(self, raw: str) -> None:
        with pytest.raises(ExecutorContractError) as caught:
            parse_heartbeat(raw)

        assert caught.value.code == "executor_unavailable"


class TestErrorCodes:
    def test_every_code_has_an_outcome(self) -> None:
        assert set(ERROR_OUTCOMES) == set(get_args(ExecutorErrorCode))

    @pytest.mark.parametrize(
        ("error", "code", "outcome"),
        [
            (OfficeInputError(), "invalid_input", "not_executed"),
            (OfficeFileExistsError(), "file_exists", "not_executed"),
            (OfficeUnsavedChangesError(), "unsaved_changes", "not_executed"),
            (OfficeNotInstalledError(), "not_installed", "not_executed"),
            (OfficeBusyError(), "busy", "not_executed"),
            (OfficeFileLockedError(), "file_locked", "not_executed"),
            (OfficeTimeoutError(), "timeout", "unknown"),
            (OfficeComError(), "com_error", "unknown"),
            (OfficeError(), "com_error", "unknown"),
        ],
    )
    def test_office_exceptions_map_to_typed_codes(
        self, error: OfficeError, code: str, outcome: str
    ) -> None:
        assert error_code_for(error) == code
        assert ExecutorError(code=error_code_for(error)).outcome == outcome

    def test_a_subclass_inherits_the_code_of_its_parent(self) -> None:
        class OfficeSpecialLockError(OfficeFileLockedError):
            pass

        assert error_code_for(OfficeSpecialLockError()) == "file_locked"

    def test_every_office_exception_in_core_has_an_explicit_code(self) -> None:
        specific = [
            value
            for _, value in inspect.getmembers(exceptions, inspect.isclass)
            if issubclass(value, OfficeError) and value not in (OfficeError, OfficeComError)
        ]

        assert specific
        for cls in specific:
            assert error_code_for(cls()) != "com_error", cls.__name__

    def test_an_office_timeout_is_never_reported_as_not_executed(self) -> None:
        assert ERROR_OUTCOMES["timeout"] == "unknown"
        assert ERROR_OUTCOMES["reply_timeout"] == "unknown"


class TestAlignmentWithTheController:
    def test_wire_caps_match_the_controller_limits(self) -> None:
        assert MAX_CELLS == excel_validation.MAX_WRITE_CELLS
        assert MAX_CELL_TEXT_CHARS == excel_validation.MAX_CELL_TEXT_CHARS
        assert MAX_SHEET_CHARS == excel_validation.MAX_SHEET_NAME_CHARS
        assert MAX_PATH_CHARS >= excel_validation.MAX_PATH_CHARS

    def test_the_default_budget_outlives_the_controller_timeout_and_fits_the_window(self) -> None:
        assert excel_controller.CALL_TIMEOUT_S < DEFAULT_CALL_BUDGET_S <= MAX_CALL_WINDOW_S

    def test_the_request_adapter_round_trips_the_default_operation_field(self) -> None:
        request = _write_request()

        assert REQUEST_ADAPTER.dump_python(request, mode="json")["operation"] == "excel.write_range"
