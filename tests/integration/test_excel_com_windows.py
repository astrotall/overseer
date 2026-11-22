from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.executor.excel.com_backend import SYNCHRONIZE, Win32ExcelBackend
from apps.executor.excel.controller import ExcelController
from libs.core.exceptions import OfficeFileLockedError

pytestmark = pytest.mark.windows

WAIT_OBJECT_0 = 0


@pytest.fixture
def win32() -> Iterator[Any]:
    import pythoncom
    import win32api
    import win32com.client
    import win32event

    pythoncom.CoInitialize()
    try:
        yield type(
            "Win32",
            (),
            {"client": win32com.client, "api": win32api, "event": win32event},
        )
    finally:
        pythoncom.CoUninitialize()


@pytest.fixture
def user_excel(win32: Any) -> Iterator[Any]:
    app = win32.client.DispatchEx("Excel.Application")
    app.Workbooks.Add()
    try:
        yield app
    finally:
        app.DisplayAlerts = False
        app.Quit()


def _process_exited(win32: Any, pid: int) -> bool:
    try:
        handle = win32.api.OpenProcess(SYNCHRONIZE, False, pid)
    except Exception:
        return True
    try:
        return bool(win32.event.WaitForSingleObject(handle, 5_000) == WAIT_OBJECT_0)
    finally:
        win32.api.CloseHandle(handle)


def _read_back(win32: Any, path: Path, cell: str) -> tuple[object, bool]:
    app = win32.client.DispatchEx("Excel.Application")
    app.DisplayAlerts = False
    try:
        workbook = app.Workbooks.Open(str(path), ReadOnly=True)
        target = workbook.Worksheets(1).Range(cell)
        value, has_formula = target.Value, bool(target.HasFormula)
        workbook.Close(SaveChanges=False)
        return value, has_formula
    finally:
        app.Quit()


async def test_a_workbook_is_created_written_and_saved(tmp_path: Path) -> None:
    path = tmp_path / "report.xlsx"

    async with ExcelController(Win32ExcelBackend()) as excel:
        info = await excel.open_or_create_workbook(str(path))
        written = await excel.write_range("Итоги", "AA1", [["имя", 1], [True, None]])
        saved = await excel.save()

    assert info.created is True
    assert written.address == "AA1:AB2"
    assert written.sheet_created is True
    assert saved.saved_as is True
    assert path.is_file()

    async with ExcelController(Win32ExcelBackend()) as excel:
        reopened = await excel.open_or_create_workbook(str(path))
        await excel.write_range("Итоги", "A1", [[42]])
        in_place = await excel.save()

    assert reopened.created is False
    assert in_place.saved_as is False


async def test_formula_text_stays_literal_unless_opted_in(win32: Any, tmp_path: Path) -> None:
    path = tmp_path / "formulas.xlsx"

    async with ExcelController(Win32ExcelBackend()) as excel:
        await excel.open_or_create_workbook(str(path))
        sheet = (await excel.write_range("Data", "A1", [["=1+1"]])).sheet
        await excel.write_range(sheet, "B1", [["=1+1"]], formulas=True)
        await excel.save()

    assert _read_back(win32, path, "A1") == ("=1+1", False)
    assert _read_back(win32, path, "B1") == (2.0, True)


async def test_no_excel_process_is_left_behind(win32: Any) -> None:
    backend = Win32ExcelBackend()

    async with ExcelController(backend) as excel:
        await excel.open_or_create_workbook(None)
        await excel.write_range("Sheet", "A1", [["x"]])
        pid = backend.pid

    assert pid is not None
    assert _process_exited(win32, pid)


async def test_the_users_own_excel_is_left_untouched(user_excel: Any, tmp_path: Path) -> None:
    alerts_before = bool(user_excel.DisplayAlerts)

    async with ExcelController(Win32ExcelBackend()) as excel:
        await excel.open_or_create_workbook(str(tmp_path / "mine.xlsx"))
        await excel.write_range("Sheet", "A1", [[1]])
        await excel.save()

    assert int(user_excel.Workbooks.Count) == 1
    assert bool(user_excel.DisplayAlerts) is alerts_before


async def test_a_file_open_in_another_excel_is_reported_as_locked(
    user_excel: Any, tmp_path: Path
) -> None:
    path = tmp_path / "busy.xlsx"
    user_excel.Workbooks(1).SaveAs(str(path))

    async with ExcelController(Win32ExcelBackend()) as excel:
        with pytest.raises(OfficeFileLockedError):
            await excel.open_or_create_workbook(str(path))
