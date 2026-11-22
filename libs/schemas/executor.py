from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

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

SCHEMA_VERSION: Final = 1

MAX_REQUEST_BYTES: Final = 1_048_576
MAX_REPLY_BYTES: Final = 65_536
MAX_HEARTBEAT_BYTES: Final = 4_096
MAX_CALL_WINDOW_S: Final = 120.0

MAX_CELLS: Final = 10_000
MAX_CELL_TEXT_CHARS: Final = 32_767
MAX_PATH_CHARS: Final = 260
MAX_SHEET_CHARS: Final = 31
MAX_CELL_REF_CHARS: Final = 16
MAX_OPERATION_CHARS: Final = 64
MAX_ADVERTISED_OPERATIONS: Final = 64
MAX_REPORTED_ERRORS: Final = 10

OpenOrCreateWorkbookOperation = Literal["excel.open_or_create_workbook"]
WriteRangeOperation = Literal["excel.write_range"]
SaveWorkbookOperation = Literal["excel.save"]

OPERATIONS: Final[frozenset[str]] = frozenset(
    {"excel.open_or_create_workbook", "excel.write_range", "excel.save"}
)

ExecutorStatus = Literal["ok", "error"]
ErrorOutcome = Literal["not_executed", "unknown"]

ExecutorErrorCode = Literal[
    "executor_unavailable",
    "transport_unavailable",
    "not_picked_up",
    "reply_timeout",
    "malformed_reply",
    "transport_error",
    "payload_too_large",
    "malformed_request",
    "unsupported_version",
    "unknown_operation",
    "deadline_exceeded",
    "path_not_allowed",
    "internal_error",
    "invalid_input",
    "file_exists",
    "unsaved_changes",
    "not_installed",
    "busy",
    "file_locked",
    "timeout",
    "com_error",
]

ERROR_OUTCOMES: Final[Mapping[ExecutorErrorCode, ErrorOutcome]] = {
    "executor_unavailable": "not_executed",
    "transport_unavailable": "not_executed",
    "not_picked_up": "not_executed",
    "reply_timeout": "unknown",
    "malformed_reply": "unknown",
    "transport_error": "unknown",
    "payload_too_large": "not_executed",
    "malformed_request": "not_executed",
    "unsupported_version": "not_executed",
    "unknown_operation": "not_executed",
    "deadline_exceeded": "not_executed",
    "path_not_allowed": "not_executed",
    "internal_error": "unknown",
    "invalid_input": "not_executed",
    "file_exists": "not_executed",
    "unsaved_changes": "not_executed",
    "not_installed": "not_executed",
    "busy": "not_executed",
    "file_locked": "not_executed",
    "timeout": "unknown",
    "com_error": "unknown",
}

_OFFICE_ERROR_CODES: Final[Mapping[type[OfficeError], ExecutorErrorCode]] = {
    OfficeInputError: "invalid_input",
    OfficeFileExistsError: "file_exists",
    OfficeUnsavedChangesError: "unsaved_changes",
    OfficeNotInstalledError: "not_installed",
    OfficeBusyError: "busy",
    OfficeFileLockedError: "file_locked",
    OfficeTimeoutError: "timeout",
    OfficeComError: "com_error",
    OfficeError: "com_error",
}

WirePath = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_PATH_CHARS)]
SheetName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_SHEET_CHARS)]
CellReference = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=MAX_CELL_REF_CHARS)
]
OperationName = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=MAX_OPERATION_CHARS)
]
CellText = Annotated[str, StringConstraints(strict=True, max_length=MAX_CELL_TEXT_CHARS)]
CellValue = StrictBool | StrictInt | FiniteFloat | CellText | None
CellRow = Annotated[list[CellValue], Field(min_length=1, max_length=MAX_CELLS)]


def _reject_bool(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("schema_version обязан быть целым числом, а не bool")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_reject_bool)]


class ExecutorContractError(ValueError):
    def __init__(
        self,
        code: ExecutorErrorCode,
        *,
        request_id: uuid.UUID | None = None,
        errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"Нарушен контракт executor: {code}")
        self.code: ExecutorErrorCode = code
        self.request_id = request_id
        self.errors = errors


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenOrCreateWorkbookArguments(_Contract):
    path: WirePath | None = None


class WriteRangeArguments(_Contract):
    sheet: SheetName
    start_cell: CellReference
    data: Annotated[list[CellRow], Field(min_length=1, max_length=MAX_CELLS)]
    formulas: StrictBool = False

    @model_validator(mode="after")
    def _check_cell_count(self) -> Self:
        if sum(len(row) for row in self.data) > MAX_CELLS:
            raise ValueError(f"не больше {MAX_CELLS} ячеек за один вызов")
        return self


class SaveWorkbookArguments(_Contract):
    path: WirePath | None = None
    overwrite: StrictBool = False


class _RequestBase(_Contract):
    schema_version: SchemaVersion = SCHEMA_VERSION
    id: uuid.UUID
    conversation_id: uuid.UUID
    issued_at: AwareDatetime
    deadline: AwareDatetime

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        window = (self.deadline - self.issued_at).total_seconds()
        if window <= 0:
            raise ValueError("deadline обязан быть позже issued_at")
        if window > MAX_CALL_WINDOW_S:
            raise ValueError(f"окно вызова не может превышать {MAX_CALL_WINDOW_S:g} с")
        return self


class OpenOrCreateWorkbookRequest(_RequestBase):
    operation: OpenOrCreateWorkbookOperation = "excel.open_or_create_workbook"
    arguments: OpenOrCreateWorkbookArguments


class WriteRangeRequest(_RequestBase):
    operation: WriteRangeOperation = "excel.write_range"
    arguments: WriteRangeArguments


class SaveWorkbookRequest(_RequestBase):
    operation: SaveWorkbookOperation = "excel.save"
    arguments: SaveWorkbookArguments


ExecutorRequest = Annotated[
    OpenOrCreateWorkbookRequest | WriteRangeRequest | SaveWorkbookRequest,
    Field(discriminator="operation"),
]


class WorkbookOpened(_Contract):
    path: str | None
    created: StrictBool


class RangeWritten(_Contract):
    sheet: str
    address: str
    rows: StrictInt
    columns: StrictInt
    sheet_created: StrictBool


class WorkbookSaved(_Contract):
    path: str
    saved_as: StrictBool


class ExecutorError(_Contract):
    code: ExecutorErrorCode

    @property
    def outcome(self) -> ErrorOutcome:
        return ERROR_OUTCOMES[self.code]


class ExecutorAccepted(_Contract):
    type: Literal["accepted"] = "accepted"
    schema_version: SchemaVersion = SCHEMA_VERSION
    id: uuid.UUID


class ExecutorResponse(_Contract):
    type: Literal["result"] = "result"
    schema_version: SchemaVersion = SCHEMA_VERSION
    id: uuid.UUID
    status: ExecutorStatus
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error: ExecutorError | None = None

    @model_validator(mode="after")
    def _check_status_shape(self) -> Self:
        if self.status == "error":
            if self.error is None:
                raise ValueError("status='error' требует error")
            if self.data:
                raise ValueError("data допустим только при status='ok'")
        elif self.error is not None:
            raise ValueError("error допустим только при status='error'")
        return self

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @classmethod
    def ok(cls, request_id: uuid.UUID, data: dict[str, JsonValue] | None = None) -> Self:
        return cls(id=request_id, status="ok", data=data or {})

    @classmethod
    def failed(cls, request_id: uuid.UUID, code: ExecutorErrorCode) -> Self:
        return cls(id=request_id, status="error", error=ExecutorError(code=code))


ExecutorReply = Annotated[
    ExecutorAccepted | ExecutorResponse,
    Field(discriminator="type"),
]


class ExecutorHeartbeat(_Contract):
    schema_version: SchemaVersion = SCHEMA_VERSION
    instance_id: uuid.UUID
    started_at: AwareDatetime
    sent_at: AwareDatetime
    operations: Annotated[list[OperationName], Field(max_length=MAX_ADVERTISED_OPERATIONS)]


REQUEST_ADAPTER: Final[TypeAdapter[ExecutorRequest]] = TypeAdapter(ExecutorRequest)
REPLY_ADAPTER: Final[TypeAdapter[ExecutorReply]] = TypeAdapter(ExecutorReply)


def error_code_for(error: OfficeError) -> ExecutorErrorCode:
    for cls in type(error).__mro__:
        code = _OFFICE_ERROR_CODES.get(cls)
        if code is not None:
            return code
    return "com_error"


def encode_request(request: ExecutorRequest) -> str:
    return _encode(request, MAX_REQUEST_BYTES, request_id=request.id)


def encode_reply(reply: ExecutorReply) -> str:
    return _encode(reply, MAX_REPLY_BYTES, request_id=reply.id)


def encode_heartbeat(heartbeat: ExecutorHeartbeat) -> str:
    return _encode(heartbeat, MAX_HEARTBEAT_BYTES, request_id=None)


def parse_request(raw: str | bytes) -> ExecutorRequest:
    document = _load(raw, MAX_REQUEST_BYTES, oversized="payload_too_large")
    request_id = _extract_id(document)
    version = document.get("schema_version")
    if not _is_version(version):
        raise ExecutorContractError("malformed_request", request_id=request_id)
    if version != SCHEMA_VERSION:
        raise ExecutorContractError("unsupported_version", request_id=request_id)
    operation = document.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ExecutorContractError("unknown_operation", request_id=request_id)
    try:
        return REQUEST_ADAPTER.validate_python(document)
    except ValidationError as exc:
        raise ExecutorContractError(
            "malformed_request", request_id=request_id, errors=_error_types(exc)
        ) from None


def parse_reply(raw: str | bytes) -> ExecutorReply:
    document = _load(raw, MAX_REPLY_BYTES, oversized="malformed_reply")
    request_id = _extract_id(document)
    try:
        return REPLY_ADAPTER.validate_python(document)
    except ValidationError as exc:
        raise ExecutorContractError(
            "malformed_reply", request_id=request_id, errors=_error_types(exc)
        ) from None


def parse_heartbeat(raw: str | bytes) -> ExecutorHeartbeat:
    document = _load(raw, MAX_HEARTBEAT_BYTES, oversized="executor_unavailable")
    version = document.get("schema_version")
    if not _is_version(version):
        raise ExecutorContractError("executor_unavailable")
    if version != SCHEMA_VERSION:
        raise ExecutorContractError("unsupported_version")
    try:
        return ExecutorHeartbeat.model_validate(document)
    except ValidationError as exc:
        raise ExecutorContractError("executor_unavailable", errors=_error_types(exc)) from None


def _encode(model: BaseModel, limit: int, *, request_id: uuid.UUID | None) -> str:
    payload = model.model_dump_json()
    if len(payload.encode("utf-8")) > limit:
        raise ExecutorContractError("payload_too_large", request_id=request_id)
    return payload


def _load(raw: str | bytes, limit: int, *, oversized: ExecutorErrorCode) -> dict[str, Any]:
    data = raw.encode("utf-8") if isinstance(raw, str) and len(raw) <= limit else raw
    if len(data) > limit:
        raise ExecutorContractError(oversized)
    malformed: ExecutorErrorCode = (
        "malformed_request" if oversized == "payload_too_large" else oversized
    )
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        raise ExecutorContractError(malformed) from None
    if not isinstance(document, dict):
        raise ExecutorContractError(malformed)
    return document


def _extract_id(document: Mapping[str, Any]) -> uuid.UUID | None:
    value = document.get("id")
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _is_version(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _error_types(exc: ValidationError) -> tuple[str, ...]:
    return tuple(error["type"] for error in exc.errors()[:MAX_REPORTED_ERRORS])
