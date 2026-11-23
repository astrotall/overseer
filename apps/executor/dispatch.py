from __future__ import annotations

from typing import Final, assert_never

from pydantic import JsonValue

from apps.executor.excel.sessions import ConversationWorkbooks
from apps.executor.paths import PathPolicy
from libs.schemas.executor import (
    OPERATIONS,
    ExecutorRequest,
    OpenOrCreateWorkbookRequest,
    RangeWritten,
    SaveWorkbookRequest,
    WorkbookOpened,
    WorkbookSaved,
    WriteRangeRequest,
)

EXCEL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"excel.open_or_create_workbook", "excel.write_range", "excel.save"}
)


class ExcelOperations:
    def __init__(self, workbooks: ConversationWorkbooks, *, paths: PathPolicy) -> None:
        self._workbooks = workbooks
        self._paths = paths

    async def start(self) -> None:
        await self._workbooks.start()

    async def aclose(self) -> None:
        await self._workbooks.aclose()

    async def idle(self) -> None:
        await self._workbooks.sweep()

    async def open(self, request: OpenOrCreateWorkbookRequest) -> WorkbookOpened:
        path = self._allowed(request.arguments.path)
        info = await self._workbooks.open(request.conversation_id, path)
        return WorkbookOpened(path=info.path, created=info.created)

    async def write(self, request: WriteRangeRequest) -> RangeWritten:
        arguments = request.arguments
        result = await self._workbooks.write(
            request.conversation_id,
            arguments.sheet,
            arguments.start_cell,
            arguments.data,
            formulas=arguments.formulas,
        )
        return RangeWritten(
            sheet=result.sheet,
            address=result.address,
            rows=result.rows,
            columns=result.columns,
            sheet_created=result.sheet_created,
        )

    async def save(self, request: SaveWorkbookRequest) -> WorkbookSaved:
        arguments = request.arguments
        result = await self._workbooks.save(
            request.conversation_id, self._allowed(arguments.path), overwrite=arguments.overwrite
        )
        return WorkbookSaved(path=result.path, saved_as=result.saved_as)

    def _allowed(self, path: str | None) -> str | None:
        return None if path is None else self._paths.check(path)


class ExecutorDispatcher:
    def __init__(self, *, excel: ExcelOperations) -> None:
        self._excel = excel

    @property
    def operations(self) -> frozenset[str]:
        return EXCEL_OPERATIONS & OPERATIONS

    async def start(self) -> None:
        await self._excel.start()

    async def aclose(self) -> None:
        await self._excel.aclose()

    async def idle(self) -> None:
        await self._excel.idle()

    async def dispatch(self, request: ExecutorRequest) -> dict[str, JsonValue]:
        result: WorkbookOpened | RangeWritten | WorkbookSaved
        match request:
            case OpenOrCreateWorkbookRequest():
                result = await self._excel.open(request)
            case WriteRangeRequest():
                result = await self._excel.write(request)
            case SaveWorkbookRequest():
                result = await self._excel.save(request)
            case _:
                assert_never(request)
        return result.model_dump(mode="json")
