from __future__ import annotations

import gc
import os
from contextlib import AbstractContextManager
from typing import Any, Final

from apps.executor.com import Pywin32, load_pywin32, translate_com_errors
from apps.executor.excel.backend import WorkbookHandle
from apps.executor.excel.validation import CellRows, CellValue
from apps.executor.sta import ThreadAffinity
from libs.core.exceptions import OfficeError, OfficeFileLockedError
from libs.core.logging import get_logger

logger = get_logger(__name__)

ComObject = Any

PROG_ID: Final = "Excel.Application"
APPLICATION: Final = "Excel"

XL_UPDATE_LINKS_NEVER: Final = 0
MSO_AUTOMATION_SECURITY_FORCE_DISABLE: Final = 3
INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1

SYNCHRONIZE: Final = 0x00100000
PROCESS_TERMINATE: Final = 0x0001
WAIT_TIMEOUT: Final = 0x00000102
REAP_TIMEOUT_MS: Final = 5_000

READ_ONLY_TEXT: Final = (
    "Книга открылась только для чтения: файл занят другим процессом или защищён от записи"
)

AUTOMATION_SETTINGS: Final[tuple[tuple[str, object], ...]] = (
    ("DisplayAlerts", False),
    ("AskToUpdateLinks", False),
    ("EnableEvents", False),
    ("AutomationSecurity", MSO_AUTOMATION_SECURITY_FORCE_DISABLE),
)


class Win32ExcelBackend:
    def __init__(self) -> None:
        self._affinity = ThreadAffinity()
        self._win32: Pywin32 | None = None
        self._app: ComObject | None = None
        self._saved_settings: dict[str, object] = {}
        self._workbooks: dict[WorkbookHandle, ComObject] = {}
        self._next_handle = 1
        self._process: Any = None
        self._pid: int | None = None
        self._quit_issued = False

    @property
    def pid(self) -> int | None:
        return self._pid

    def enter_thread(self) -> None:
        win32 = load_pywin32()
        self._affinity.bind()
        try:
            win32.pythoncom.CoInitialize()
        except BaseException:
            self._affinity.release()
            raise
        self._win32 = win32

    def exit_thread(self) -> None:
        win32 = self._win32
        if win32 is None:
            self._affinity.release()
            return

        try:
            self._shutdown()
        except Exception:
            logger.exception("excel.shutdown_failed")
        finally:
            self._workbooks.clear()
            self._app = None
            gc.collect()
            win32.pythoncom.CoUninitialize()
            self._win32 = None
            self._reap(win32)
            self._affinity.release()

    def pump(self) -> None:
        win32 = self._win32
        if win32 is not None:
            win32.pythoncom.PumpWaitingMessages()

    def path_exists(self, path: str) -> bool:
        self._affinity.check()
        return os.path.isfile(path)

    def open_workbook(self, path: str) -> WorkbookHandle:
        self._affinity.check()
        app = self._application()
        with self._translating("open"):
            workbook = app.Workbooks.Open(
                path,
                UpdateLinks=XL_UPDATE_LINKS_NEVER,
                ReadOnly=False,
                IgnoreReadOnlyRecommended=True,
                Notify=False,
                AddToMru=False,
            )
            read_only = bool(workbook.ReadOnly)

        if read_only:
            self._close_quietly(workbook)
            raise OfficeFileLockedError(READ_ONLY_TEXT)
        return self._register(workbook)

    def create_workbook(self) -> WorkbookHandle:
        self._affinity.check()
        app = self._application()
        with self._translating("create"):
            workbook = app.Workbooks.Add()
        return self._register(workbook)

    def close_workbook(self, workbook: WorkbookHandle) -> None:
        self._affinity.check()
        with self._translating("close"):
            self._workbooks[workbook].Close(SaveChanges=False)
        del self._workbooks[workbook]

    def is_dirty(self, workbook: WorkbookHandle) -> bool:
        self._affinity.check()
        with self._translating("is_dirty"):
            return not bool(self._workbooks[workbook].Saved)

    def sheet_names(self, workbook: WorkbookHandle) -> list[str]:
        self._affinity.check()
        with self._translating("sheet_names"):
            sheets = self._workbooks[workbook].Worksheets
            return [str(sheets(index).Name) for index in range(1, int(sheets.Count) + 1)]

    def add_sheet(self, workbook: WorkbookHandle, name: str) -> None:
        self._affinity.check()
        with self._translating("add_sheet"):
            sheets = self._workbooks[workbook].Worksheets
            sheet = sheets.Add(After=sheets(int(sheets.Count)))
            sheet.Name = name

    def write_range(
        self, workbook: WorkbookHandle, sheet: str, address: str, values: CellRows
    ) -> None:
        self._affinity.check()
        win32 = self._require()
        converted = tuple(tuple(_to_com(value, win32) for value in row) for row in values)
        with self._translating("write_range"):
            target = self._workbooks[workbook].Worksheets(sheet).Range(address)
            target.Value = converted

    def save(self, workbook: WorkbookHandle) -> None:
        self._affinity.check()
        with self._translating("save"):
            self._workbooks[workbook].Save()

    def save_as(self, workbook: WorkbookHandle, path: str, file_format: int) -> None:
        self._affinity.check()
        with self._translating("save_as"):
            self._workbooks[workbook].SaveAs(path, FileFormat=file_format, AddToMru=False)

    def _require(self) -> Pywin32:
        win32 = self._win32
        if win32 is None:
            raise RuntimeError("COM apartment is not initialized: enter_thread() not called")
        return win32

    def _translating(self, operation: str) -> AbstractContextManager[None]:
        return translate_com_errors(
            self._require().pywintypes.com_error, application=APPLICATION, operation=operation
        )

    def _application(self) -> ComObject:
        if self._app is not None:
            return self._app

        win32 = self._require()
        with self._translating("start"):
            app = win32.client.DispatchEx(PROG_ID)

        try:
            self._track_process(win32, app)
            with self._translating("configure"):
                app.Visible = False
                for name, value in AUTOMATION_SETTINGS:
                    self._saved_settings[name] = getattr(app, name)
                    setattr(app, name, value)
        except BaseException:
            self._saved_settings.clear()
            self._quit_quietly(app)
            raise

        self._app = app
        logger.info("excel.instance_started", pid=self._pid)
        return app

    def _track_process(self, win32: Pywin32, app: ComObject) -> None:
        try:
            with self._translating("hwnd"):
                hwnd = int(app.Hwnd)
            _, pid = win32.win32process.GetWindowThreadProcessId(hwnd)
            self._process = win32.win32api.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid)
            self._pid = int(pid)
        except (OfficeError, win32.pywintypes.error):
            logger.warning("excel.process_not_tracked")

    def _register(self, workbook: ComObject) -> WorkbookHandle:
        handle = WorkbookHandle(self._next_handle)
        self._next_handle += 1
        self._workbooks[handle] = workbook
        return handle

    def _close_quietly(self, workbook: ComObject) -> None:
        try:
            with self._translating("close"):
                workbook.Close(SaveChanges=False)
        except OfficeError:
            logger.warning("excel.workbook_close_failed")

    def _quit_quietly(self, app: ComObject) -> None:
        try:
            with self._translating("quit"):
                app.Quit()
            self._quit_issued = True
        except OfficeError:
            logger.warning("excel.quit_failed")

    def _shutdown(self) -> None:
        app = self._app
        if app is None:
            return

        unsaved = 0
        for handle, workbook in list(self._workbooks.items()):
            try:
                with self._translating("close"):
                    if not bool(workbook.Saved):
                        unsaved += 1
                        continue
                    workbook.Close(SaveChanges=False)
                del self._workbooks[handle]
            except OfficeError:
                logger.warning("excel.workbook_close_failed")

        with self._translating("count_workbooks"):
            foreign = int(app.Workbooks.Count) - len(self._workbooks)

        if unsaved:
            logger.warning("excel.unsaved_workbooks_kept", unsaved=unsaved, pid=self._pid)
        if foreign > 0 or unsaved:
            self._hand_over(app, foreign)
            return
        self._quit_quietly(app)

    def _hand_over(self, app: ComObject, foreign: int) -> None:
        logger.warning("excel.instance_handed_over", foreign_workbooks=foreign, pid=self._pid)
        with self._translating("hand_over"):
            for name, value in self._saved_settings.items():
                setattr(app, name, value)
            app.Visible = True
            app.UserControl = True
        self._saved_settings.clear()

    def _reap(self, win32: Pywin32) -> None:
        process, self._process = self._process, None
        pid, self._pid = self._pid, None
        quit_issued, self._quit_issued = self._quit_issued, False
        if process is None:
            return

        try:
            if quit_issued:
                status = win32.win32event.WaitForSingleObject(process, REAP_TIMEOUT_MS)
                if status == WAIT_TIMEOUT:
                    logger.warning("excel.orphan_terminated", pid=pid)
                    win32.win32api.TerminateProcess(process, 1)
        except win32.pywintypes.error:
            logger.exception("excel.reap_failed", pid=pid)
        finally:
            win32.win32api.CloseHandle(process)


def _to_com(value: CellValue, win32: Pywin32) -> object:
    if value is None:
        return win32.pythoncom.Empty
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    if INT32_MIN <= value <= INT32_MAX:
        return value
    return float(value)
