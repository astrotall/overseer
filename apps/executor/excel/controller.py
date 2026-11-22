from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Final, TypeVar

from apps.executor.excel.backend import ExcelBackend, WorkbookHandle
from apps.executor.excel.validation import (
    CellRows,
    CellValue,
    match_sheet,
    parse_cell,
    range_address,
    same_path,
    to_excel_payload,
    validate_rows,
    validate_sheet_name,
    validate_workbook_path,
    workbook_format,
)
from apps.executor.sta import StaThread
from libs.core.exceptions import (
    OfficeError,
    OfficeFileExistsError,
    OfficeInputError,
    OfficeTimeoutError,
    OfficeUnsavedChangesError,
)
from libs.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

APPLICATION: Final = "Excel"
CALL_TIMEOUT_S: Final = 60.0
STOP_TIMEOUT_S: Final = 30.0

NO_WORKBOOK_TEXT: Final = "Нет открытой книги: сначала откройте или создайте её"
NO_SAVE_PATH_TEXT: Final = "Новая книга ещё не сохранялась: укажите путь для сохранения"
FILE_EXISTS_TEXT: Final = (
    "Файл по этому пути уже существует; перезаписать его можно только явным запросом"
)
UNSAVED_TEXT: Final = (
    "В открытой книге есть несохранённые изменения: сохраните её, прежде чем открывать другую"
)
TIMEOUT_TEXT: Final = "Excel не ответил за {seconds:g} с; выполнилась ли операция, неизвестно"


@dataclass(frozen=True, slots=True)
class WorkbookInfo:
    path: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class WriteResult:
    sheet: str
    address: str
    rows: int
    columns: int
    sheet_created: bool


@dataclass(frozen=True, slots=True)
class SaveResult:
    path: str
    saved_as: bool


@dataclass(frozen=True, slots=True)
class _Workbook:
    handle: WorkbookHandle
    path: str | None
    target: str | None

    def bound_to(self, path: str) -> bool:
        return any(
            candidate is not None and same_path(candidate, path)
            for candidate in (self.path, self.target)
        )


def _noop() -> None:
    return None


class ExcelController:
    def __init__(
        self,
        backend: ExcelBackend,
        *,
        call_timeout_s: float | None = CALL_TIMEOUT_S,
        stop_timeout_s: float = STOP_TIMEOUT_S,
        thread_name: str = "excel-sta",
    ) -> None:
        self._backend = backend
        self._call_timeout_s = call_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._thread = StaThread(
            name=thread_name,
            initialize=backend.enter_thread,
            finalize=backend.exit_thread,
            idle=backend.pump,
        )
        self._lock = asyncio.Lock()
        self._workbook: _Workbook | None = None

    @property
    def running(self) -> bool:
        return self._thread.running

    async def start(self) -> None:
        async with self._lock:
            self._workbook = None
            self._thread.start()
            try:
                await self._call(_noop)
            except BaseException:
                await asyncio.to_thread(self._thread.stop, self._stop_timeout_s)
                raise

    async def aclose(self) -> None:
        self._workbook = None
        await asyncio.to_thread(self._thread.stop, self._stop_timeout_s)

    async def __aenter__(self) -> ExcelController:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def open_or_create_workbook(self, path: str | None) -> WorkbookInfo:
        target = None if path is None else validate_workbook_path(path)

        async with self._lock:
            current = self._workbook
            if current is not None and target is not None and current.bound_to(target):
                return WorkbookInfo(path=target, created=current.path is None)

            previous = None if current is None else current.handle
            handle, created = await self._call(
                lambda: _replace_workbook(self._backend, previous, target)
            )
            self._workbook = _Workbook(
                handle=handle,
                path=None if created else target,
                target=target if created else None,
            )

        logger.info("excel.workbook_ready", created=created, has_path=target is not None)
        return WorkbookInfo(path=target, created=created)

    async def write_range(
        self,
        sheet: str,
        start_cell: str,
        data: Sequence[Sequence[CellValue]],
        *,
        formulas: bool = False,
    ) -> WriteResult:
        name = validate_sheet_name(sheet)
        start = parse_cell(start_cell)
        rows = validate_rows(data)
        address = range_address(start, len(rows), len(rows[0]))
        payload = to_excel_payload(rows, formulas=formulas)

        async with self._lock:
            handle = self._require_workbook().handle
            actual, created = await self._call(
                lambda: _write(self._backend, handle, name, address, payload)
            )

        logger.info(
            "excel.range_written",
            rows=len(rows),
            columns=len(rows[0]),
            sheet_created=created,
            formulas=formulas,
        )
        return WriteResult(
            sheet=actual,
            address=address,
            rows=len(rows),
            columns=len(rows[0]),
            sheet_created=created,
        )

    async def save(self, path: str | None = None, *, overwrite: bool = False) -> SaveResult:
        target = None if path is None else validate_workbook_path(path)

        async with self._lock:
            workbook = self._require_workbook()
            destination = target or workbook.path or workbook.target
            if destination is None:
                raise OfficeInputError(NO_SAVE_PATH_TEXT)

            in_place = workbook.path is not None and same_path(workbook.path, destination)
            if in_place and workbook.path is not None:
                destination = workbook.path
            await self._call(
                lambda: _save(
                    self._backend,
                    workbook.handle,
                    destination,
                    in_place=in_place,
                    overwrite=overwrite,
                )
            )
            self._workbook = _Workbook(handle=workbook.handle, path=destination, target=None)

        logger.info("excel.workbook_saved", saved_as=not in_place, overwrite=overwrite)
        return SaveResult(path=destination, saved_as=not in_place)

    def _require_workbook(self) -> _Workbook:
        if self._workbook is None:
            raise OfficeInputError(NO_WORKBOOK_TEXT)
        return self._workbook

    async def _call(self, fn: Callable[[], T]) -> T:
        limit = self._call_timeout_s
        if limit is None:
            return await self._thread.call(fn)

        try:
            async with asyncio.timeout(limit) as scope:
                return await self._thread.call(fn)
        except TimeoutError as exc:
            if not scope.expired():
                raise
            logger.warning("excel.call_timed_out", timeout=limit)
            raise OfficeTimeoutError(TIMEOUT_TEXT.format(seconds=limit)) from exc


def _replace_workbook(
    backend: ExcelBackend, previous: WorkbookHandle | None, target: str | None
) -> tuple[WorkbookHandle, bool]:
    if previous is not None and backend.is_dirty(previous):
        raise OfficeUnsavedChangesError(UNSAVED_TEXT)

    if target is not None and backend.path_exists(target):
        handle, created = backend.open_workbook(target), False
    else:
        handle, created = backend.create_workbook(), True

    if previous is not None:
        try:
            backend.close_workbook(previous)
        except OfficeError:
            logger.warning("excel.previous_workbook_close_failed")
    return handle, created


def _write(
    backend: ExcelBackend,
    handle: WorkbookHandle,
    sheet: str,
    address: str,
    payload: CellRows,
) -> tuple[str, bool]:
    actual = match_sheet(backend.sheet_names(handle), sheet)
    created = actual is None
    if actual is None:
        backend.add_sheet(handle, sheet)
        actual = sheet

    backend.write_range(handle, actual, address, payload)
    return actual, created


def _save(
    backend: ExcelBackend,
    handle: WorkbookHandle,
    destination: str,
    *,
    in_place: bool,
    overwrite: bool,
) -> None:
    if in_place:
        backend.save(handle)
        return

    if not overwrite and backend.path_exists(destination):
        raise OfficeFileExistsError(FILE_EXISTS_TEXT)
    backend.save_as(handle, destination, workbook_format(destination))
