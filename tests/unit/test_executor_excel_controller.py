from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from apps.executor.excel.backend import WorkbookHandle
from apps.executor.excel.com_backend import Win32ExcelBackend
from apps.executor.excel.controller import (
    FILE_EXISTS_TEXT,
    NO_SAVE_PATH_TEXT,
    NO_WORKBOOK_TEXT,
    ExcelController,
    SaveResult,
    WorkbookInfo,
    WriteResult,
)
from apps.executor.excel.validation import (
    MAX_WRITE_CELLS,
    XL_OPEN_XML_WORKBOOK,
    XL_OPEN_XML_WORKBOOK_MACRO_ENABLED,
    CellRows,
)
from apps.executor.sta import StaThreadUnavailableError
from libs.core.exceptions import (
    ConfigurationError,
    OfficeBusyError,
    OfficeError,
    OfficeFileExistsError,
    OfficeInputError,
    OfficeTimeoutError,
    OfficeUnsavedChangesError,
)

REPORT = "C:\\Users\\me\\report.xlsx"
OTHER = "C:\\Users\\me\\other.xlsx"
WAIT_S = 2.0


@dataclass
class FakeWorkbook:
    path: str | None
    sheets: list[str] = field(default_factory=lambda: ["Лист1"])
    dirty: bool = False
    closed: bool = False
    writes: list[tuple[str, str, CellRows]] = field(default_factory=list)


class FakeExcelBackend:
    def __init__(self, *, files: set[str] | None = None) -> None:
        self.files = set(files or ())
        self.workbooks: dict[WorkbookHandle, FakeWorkbook] = {}
        self.calls: list[tuple[str, int]] = []
        self.saved: list[tuple[str, str | None, int | None]] = []
        self.fail: dict[str, BaseException] = {}
        self.block: dict[str, threading.Event] = {}
        self.entered = threading.Event()
        self.exited = threading.Event()
        self.pumps = 0
        self._next = 1

    def _record(self, name: str) -> None:
        self.calls.append((name, threading.get_ident()))
        gate = self.block.get(name)
        if gate is not None:
            gate.wait(WAIT_S * 5)
        failure = self.fail.get(name)
        if failure is not None:
            raise failure

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def threads(self) -> set[int]:
        return {ident for _, ident in self.calls}

    def enter_thread(self) -> None:
        self._record("enter_thread")
        self.entered.set()

    def exit_thread(self) -> None:
        self._record("exit_thread")
        self.exited.set()

    def pump(self) -> None:
        self.pumps += 1

    def path_exists(self, path: str) -> bool:
        self._record("path_exists")
        return path.casefold() in {known.casefold() for known in self.files}

    def open_workbook(self, path: str) -> WorkbookHandle:
        self._record("open_workbook")
        return self._register(FakeWorkbook(path=path))

    def create_workbook(self) -> WorkbookHandle:
        self._record("create_workbook")
        return self._register(FakeWorkbook(path=None))

    def close_workbook(self, workbook: WorkbookHandle) -> None:
        self._record("close_workbook")
        self.workbooks[workbook].closed = True

    def is_dirty(self, workbook: WorkbookHandle) -> bool:
        self._record("is_dirty")
        return self.workbooks[workbook].dirty

    def sheet_names(self, workbook: WorkbookHandle) -> list[str]:
        self._record("sheet_names")
        return list(self.workbooks[workbook].sheets)

    def add_sheet(self, workbook: WorkbookHandle, name: str) -> None:
        self._record("add_sheet")
        self.workbooks[workbook].sheets.append(name)

    def write_range(
        self, workbook: WorkbookHandle, sheet: str, address: str, values: CellRows
    ) -> None:
        self._record("write_range")
        book = self.workbooks[workbook]
        book.writes.append((sheet, address, values))
        book.dirty = True

    def save(self, workbook: WorkbookHandle) -> None:
        self._record("save")
        book = self.workbooks[workbook]
        self.saved.append(("save", book.path, None))
        book.dirty = False

    def save_as(self, workbook: WorkbookHandle, path: str, file_format: int) -> None:
        self._record("save_as")
        book = self.workbooks[workbook]
        self.saved.append(("save_as", path, file_format))
        book.path = path
        book.dirty = False
        self.files.add(path)

    def _register(self, workbook: FakeWorkbook) -> WorkbookHandle:
        handle = WorkbookHandle(self._next)
        self._next += 1
        self.workbooks[handle] = workbook
        return handle

    def only(self) -> FakeWorkbook:
        open_books = [book for book in self.workbooks.values() if not book.closed]
        assert len(open_books) == 1
        return open_books[0]


@pytest.fixture
def backend() -> FakeExcelBackend:
    return FakeExcelBackend(files={REPORT})


@pytest.fixture
async def excel(backend: FakeExcelBackend) -> AsyncIterator[ExcelController]:
    controller = ExcelController(backend, call_timeout_s=WAIT_S, stop_timeout_s=WAIT_S)
    await controller.start()
    yield controller
    for gate in backend.block.values():
        gate.set()
    await controller.aclose()


async def test_every_backend_call_runs_on_one_thread_that_is_not_the_loop(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)
    await excel.write_range("Лист1", "A1", [["a"]])
    await excel.save(OTHER)
    await excel.aclose()

    assert backend.names[0] == "enter_thread"
    assert backend.names[-1] == "exit_thread"
    assert len(backend.threads) == 1
    assert threading.get_ident() not in backend.threads


async def test_creating_without_a_path_gives_an_unsaved_workbook(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    info = await excel.open_or_create_workbook(None)

    assert info == WorkbookInfo(path=None, created=True)
    assert backend.names.count("create_workbook") == 1
    assert "path_exists" not in backend.names


async def test_an_existing_file_is_opened(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    info = await excel.open_or_create_workbook("c:/users/me/REPORT.xlsx")

    assert info == WorkbookInfo(path="c:\\users\\me\\REPORT.xlsx", created=False)
    assert backend.names[-1] == "open_workbook"


async def test_a_missing_file_becomes_a_new_workbook_bound_to_that_path(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    info = await excel.open_or_create_workbook(OTHER)
    saved = await excel.save()

    assert info == WorkbookInfo(path=OTHER, created=True)
    assert saved == SaveResult(path=OTHER, saved_as=True)
    assert backend.saved == [("save_as", OTHER, XL_OPEN_XML_WORKBOOK)]


async def test_reopening_the_current_file_is_a_no_op(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)
    calls = len(backend.calls)

    info = await excel.open_or_create_workbook(REPORT.upper())

    assert info.created is False
    assert len(backend.calls) == calls


async def test_switching_away_from_unsaved_changes_is_refused(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)
    await excel.write_range("Лист1", "A1", [[1]])

    with pytest.raises(OfficeUnsavedChangesError):
        await excel.open_or_create_workbook(OTHER)

    assert "create_workbook" not in backend.names
    assert backend.only().path == REPORT
    await excel.write_range("Лист1", "B1", [[2]])
    assert len(backend.only().writes) == 2


async def test_switching_away_from_a_clean_workbook_opens_the_new_one_before_closing(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)
    await excel.open_or_create_workbook(OTHER)

    tail = backend.names[-4:]
    assert tail == ["is_dirty", "path_exists", "create_workbook", "close_workbook"]
    assert backend.only().path is None


async def test_a_failed_open_keeps_the_current_workbook(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)
    backend.fail["create_workbook"] = OfficeBusyError()

    with pytest.raises(OfficeBusyError):
        await excel.open_or_create_workbook(OTHER)

    assert "close_workbook" not in backend.names
    await excel.write_range("Лист1", "A1", [[1]])
    assert backend.only().path == REPORT


async def test_a_failed_close_of_the_previous_workbook_does_not_lose_the_new_one(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)
    backend.fail["close_workbook"] = OfficeBusyError()

    info = await excel.open_or_create_workbook(OTHER)
    await excel.write_range("Лист1", "A1", [[1]])

    assert info.created is True
    new_book = next(book for book in backend.workbooks.values() if book.path is None)
    assert len(new_book.writes) == 1


async def test_writing_without_a_workbook_is_refused(excel: ExcelController) -> None:
    with pytest.raises(OfficeInputError, match=NO_WORKBOOK_TEXT):
        await excel.write_range("Лист1", "A1", [[1]])


async def test_a_write_lands_on_the_computed_range(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)

    result = await excel.write_range("лист1", "Z2", [[1, 2.5], [True, None]])

    assert result == WriteResult(
        sheet="Лист1", address="Z2:AA3", rows=2, columns=2, sheet_created=False
    )
    assert backend.only().writes == [("Лист1", "Z2:AA3", ((1, 2.5), (True, None)))]
    assert "add_sheet" not in backend.names


async def test_a_missing_sheet_is_created(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)

    result = await excel.write_range("Итоги", "AA1", [["x"]])

    assert result.sheet_created is True
    assert result.address == "AA1"
    assert backend.only().sheets == ["Лист1", "Итоги"]
    assert backend.only().writes[0][0] == "Итоги"


async def test_formula_strings_are_written_as_literal_text_by_default(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)
    data: list[list[str | int | float | bool | None]] = [
        ["=1+1", "+1", "-1", "@SUM(A1)", '=HYPERLINK("http://evil.example")', "текст", 5]
    ]

    await excel.write_range("Лист1", "A1", data)

    written = backend.only().writes[0][2][0]
    assert written == (
        "'=1+1",
        "'+1",
        "'-1",
        "'@SUM(A1)",
        '\'=HYPERLINK("http://evil.example")',
        "'текст",
        5,
    )


async def test_formulas_are_written_only_on_explicit_opt_in(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)

    await excel.write_range("Лист1", "A1", [["=SUM(B1:B3)", "+1", "Итого"]], formulas=True)

    assert backend.only().writes[0][2] == (("=SUM(B1:B3)", "'+1", "'Итого"),)


@pytest.mark.parametrize(
    ("sheet", "cell", "data"),
    [
        ("bad/name", "A1", [[1]]),
        ("Лист1", "A0", [[1]]),
        ("Лист1", "XFD1", [[1, 2]]),
        ("Лист1", "A1", [[1], [1, 2]]),
        ("Лист1", "A1", [[{"a": 1}]]),
        ("Лист1", "A1", [[0]] * (MAX_WRITE_CELLS + 1)),
    ],
)
async def test_invalid_writes_never_reach_the_backend(
    excel: ExcelController,
    backend: FakeExcelBackend,
    sheet: str,
    cell: str,
    data: list[list[object]],
) -> None:
    await excel.open_or_create_workbook(None)
    calls = len(backend.calls)

    with pytest.raises(OfficeInputError):
        await excel.write_range(sheet, cell, data)

    assert len(backend.calls) == calls


async def test_saving_a_new_workbook_without_any_path_is_refused(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)

    with pytest.raises(OfficeInputError, match=NO_SAVE_PATH_TEXT):
        await excel.save()

    assert backend.saved == []


async def test_an_opened_workbook_is_saved_in_place(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(REPORT)

    assert await excel.save() == SaveResult(path=REPORT, saved_as=False)
    assert await excel.save("c:/USERS/me/report.xlsx") == SaveResult(path=REPORT, saved_as=False)
    assert backend.saved == [("save", REPORT, None), ("save", REPORT, None)]


async def test_save_as_does_not_overwrite_an_existing_file_unless_asked(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    backend.files.add(OTHER)
    await excel.open_or_create_workbook(REPORT)

    with pytest.raises(OfficeFileExistsError, match=FILE_EXISTS_TEXT):
        await excel.save(OTHER)
    assert backend.saved == []

    result = await excel.save(OTHER, overwrite=True)

    assert result == SaveResult(path=OTHER, saved_as=True)
    assert backend.saved == [("save_as", OTHER, XL_OPEN_XML_WORKBOOK)]


async def test_a_new_workbook_bound_to_a_path_that_appeared_meanwhile_is_not_overwritten(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(OTHER)
    backend.files.add(OTHER)

    with pytest.raises(OfficeFileExistsError):
        await excel.save()


async def test_after_save_as_the_workbook_is_bound_to_the_new_path(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    macro = "C:\\Users\\me\\macro.xlsm"
    await excel.open_or_create_workbook(None)

    await excel.save(macro)
    second = await excel.save()

    assert second == SaveResult(path=macro, saved_as=False)
    assert backend.saved == [
        ("save_as", macro, XL_OPEN_XML_WORKBOOK_MACRO_ENABLED),
        ("save", macro, None),
    ]
    assert (await excel.open_or_create_workbook(macro)).created is False


async def test_a_failed_save_keeps_the_previous_binding(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)
    backend.fail["save_as"] = OfficeBusyError()

    with pytest.raises(OfficeBusyError):
        await excel.save(OTHER)

    del backend.fail["save_as"]
    with pytest.raises(OfficeInputError, match=NO_SAVE_PATH_TEXT):
        await excel.save()


async def test_backend_failures_reach_the_caller_typed(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)
    backend.fail["write_range"] = OfficeBusyError()

    with pytest.raises(OfficeBusyError):
        await excel.write_range("Лист1", "A1", [[1]])


async def test_a_hung_call_times_out_instead_of_hanging_the_caller(
    backend: FakeExcelBackend,
) -> None:
    controller = ExcelController(backend, call_timeout_s=0.1, stop_timeout_s=WAIT_S)
    await controller.start()
    gate = threading.Event()
    backend.block["create_workbook"] = gate

    try:
        with pytest.raises(OfficeTimeoutError):
            await asyncio.wait_for(controller.open_or_create_workbook(None), WAIT_S)
    finally:
        gate.set()
        await controller.aclose()

    assert backend.exited.is_set()


async def test_a_timeout_raised_inside_the_backend_is_not_reported_as_a_hung_call(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    backend.fail["create_workbook"] = TimeoutError("inside")

    with pytest.raises(TimeoutError, match="inside") as raised:
        await excel.open_or_create_workbook(None)

    assert not isinstance(raised.value, OfficeTimeoutError)


async def test_close_does_not_wait_behind_a_call_stuck_in_the_backend(
    backend: FakeExcelBackend,
) -> None:
    controller = ExcelController(backend, call_timeout_s=None, stop_timeout_s=0.1)
    await controller.start()
    gate = threading.Event()
    backend.block["create_workbook"] = gate
    stuck = asyncio.ensure_future(controller.open_or_create_workbook(None))
    await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(controller.aclose(), WAIT_S)
        outcome = await asyncio.wait_for(asyncio.gather(stuck, return_exceptions=True), WAIT_S)
    finally:
        gate.set()

    assert isinstance(outcome[0], StaThreadUnavailableError)
    await asyncio.to_thread(backend.exited.wait, WAIT_S)
    assert backend.exited.is_set()


async def test_a_failed_apartment_start_is_reported_and_the_thread_is_cleaned_up() -> None:
    backend = FakeExcelBackend()
    backend.fail["enter_thread"] = ConfigurationError("COM недоступен")
    controller = ExcelController(backend, stop_timeout_s=WAIT_S)

    with pytest.raises(ConfigurationError, match="COM недоступен"):
        await controller.start()

    assert not controller.running
    assert "exit_thread" not in backend.names

    del backend.fail["enter_thread"]
    await controller.start()
    assert controller.running
    await controller.aclose()
    assert not controller.running


async def test_the_controller_works_as_an_async_context_manager(backend: FakeExcelBackend) -> None:
    async with ExcelController(backend) as controller:
        assert controller.running
        await controller.open_or_create_workbook(None)

    assert not controller.running
    assert backend.exited.is_set()


async def test_calls_after_close_fail_loudly(backend: FakeExcelBackend) -> None:
    controller = ExcelController(backend)
    await controller.start()
    await controller.aclose()

    with pytest.raises(OfficeInputError):
        await controller.write_range("Лист1", "A1", [[1]])
    with pytest.raises(RuntimeError):
        await controller.open_or_create_workbook(None)


async def test_concurrent_operations_are_serialized(
    excel: ExcelController, backend: FakeExcelBackend
) -> None:
    await excel.open_or_create_workbook(None)

    await asyncio.gather(*(excel.write_range("Лист1", f"A{row}", [[row]]) for row in range(1, 21)))

    assert [write[1] for write in backend.only().writes] == [f"A{row}" for row in range(1, 21)]


def test_office_errors_share_one_base() -> None:
    assert issubclass(OfficeTimeoutError, OfficeError)
    assert issubclass(OfficeInputError, OfficeError)


@pytest.mark.skipif(sys.platform == "win32", reason="проверяет отказ вне Windows")
async def test_the_real_backend_refuses_to_start_off_windows() -> None:
    controller = ExcelController(Win32ExcelBackend(), stop_timeout_s=WAIT_S)

    with pytest.raises(ConfigurationError, match="только на Windows"):
        await controller.start()

    assert not controller.running
