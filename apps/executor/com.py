from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Final

from libs.core.exceptions import (
    ConfigurationError,
    OfficeBusyError,
    OfficeComError,
    OfficeError,
    OfficeFileLockedError,
    OfficeNotInstalledError,
)
from libs.core.logging import get_logger

logger = get_logger(__name__)

NOT_WINDOWS_TEXT: Final = (
    "Автоматизация Office через COM работает только на Windows в сессии пользователя; "
    "на этой платформе ({platform}) она недоступна"
)
PYWIN32_MISSING_TEXT: Final = (
    "pywin32 не установлен: автоматизация Office недоступна. "
    "Поставьте группу executor — uv sync --group executor"
)

DISP_E_EXCEPTION: Final = 0x80020009
REGDB_E_CLASSNOTREG: Final = 0x80040154
CO_E_CLASSSTRING: Final = 0x800401F3
RPC_E_CALL_REJECTED: Final = 0x80010001
RPC_E_SERVERCALL_RETRYLATER: Final = 0x8001010A
VBA_E_IGNORE: Final = 0x800AC472
E_ACCESSDENIED: Final = 0x80070005
HRESULT_SHARING_VIOLATION: Final = 0x80070020
HRESULT_LOCK_VIOLATION: Final = 0x80070021
STG_E_ACCESSDENIED: Final = 0x80030005
STG_E_SHAREVIOLATION: Final = 0x80030020
STG_E_LOCKVIOLATION: Final = 0x80030021

HRESULT_ERRORS: Final[Mapping[int, type[OfficeError]]] = {
    REGDB_E_CLASSNOTREG: OfficeNotInstalledError,
    CO_E_CLASSSTRING: OfficeNotInstalledError,
    RPC_E_CALL_REJECTED: OfficeBusyError,
    RPC_E_SERVERCALL_RETRYLATER: OfficeBusyError,
    VBA_E_IGNORE: OfficeBusyError,
    E_ACCESSDENIED: OfficeFileLockedError,
    HRESULT_SHARING_VIOLATION: OfficeFileLockedError,
    HRESULT_LOCK_VIOLATION: OfficeFileLockedError,
    STG_E_ACCESSDENIED: OfficeFileLockedError,
    STG_E_SHAREVIOLATION: OfficeFileLockedError,
    STG_E_LOCKVIOLATION: OfficeFileLockedError,
}

ERROR_TEXTS: Final[Mapping[type[OfficeError], str]] = {
    OfficeNotInstalledError: (
        "{application} не установлен или не зарегистрирован в COM на этой машине"
    ),
    OfficeBusyError: (
        "{application} занят (например, в нём идёт редактирование ячейки или открыт диалог) "
        "и отклонил вызов; его можно повторить позже"
    ),
    OfficeFileLockedError: "Файл занят другим процессом или доступен только для чтения",
    OfficeComError: "{application} завершил вызов ошибкой. Детали остались в логах сервера",
}


@dataclass(frozen=True, slots=True)
class Pywin32:
    pythoncom: ModuleType
    pywintypes: ModuleType
    client: ModuleType
    win32api: ModuleType
    win32event: ModuleType
    win32process: ModuleType


def load_pywin32(platform: str = sys.platform) -> Pywin32:
    if platform != "win32":
        raise ConfigurationError(NOT_WINDOWS_TEXT.format(platform=platform))

    try:
        import pythoncom
        import pywintypes
        import win32api
        import win32com.client
        import win32event
        import win32process
    except ImportError as exc:
        raise ConfigurationError(PYWIN32_MISSING_TEXT) from exc

    return Pywin32(
        pythoncom=pythoncom,
        pywintypes=pywintypes,
        client=win32com.client,
        win32api=win32api,
        win32event=win32event,
        win32process=win32process,
    )


def normalize_hresult(value: int) -> int:
    return value & 0xFFFFFFFF


def format_hresult(value: int | None) -> str | None:
    return None if value is None else f"0x{value:08X}"


@dataclass(frozen=True, slots=True)
class ComErrorDetails:
    hresult: int | None
    scode: int | None
    source: str | None
    description: str | None

    @property
    def effective(self) -> int | None:
        if self.hresult == DISP_E_EXCEPTION and self.scode is not None:
            return self.scode
        return self.hresult


def com_error_details(error: BaseException) -> ComErrorDetails:
    args = error.args
    hresult = getattr(error, "hresult", None)
    if not isinstance(hresult, int) and args and isinstance(args[0], int):
        hresult = args[0]

    excepinfo = getattr(error, "excepinfo", None)
    if excepinfo is None and len(args) > 2:
        excepinfo = args[2]

    source: str | None = None
    description: str | None = None
    scode: int | None = None
    if isinstance(excepinfo, tuple) and len(excepinfo) >= 6:
        raw_source, raw_description, raw_scode = excepinfo[1], excepinfo[2], excepinfo[5]
        source = raw_source if isinstance(raw_source, str) else None
        description = raw_description if isinstance(raw_description, str) else None
        scode = normalize_hresult(raw_scode) if isinstance(raw_scode, int) and raw_scode else None

    return ComErrorDetails(
        hresult=normalize_hresult(hresult) if isinstance(hresult, int) else None,
        scode=scode,
        source=source,
        description=description,
    )


def map_com_error(error: BaseException, *, application: str) -> OfficeError:
    code = com_error_details(error).effective
    error_type = OfficeComError if code is None else HRESULT_ERRORS.get(code, OfficeComError)
    return error_type(ERROR_TEXTS[error_type].format(application=application), hresult=code)


@contextmanager
def translate_com_errors(
    com_error: type[BaseException], *, application: str, operation: str
) -> Iterator[None]:
    try:
        yield
    except com_error as exc:
        mapped = map_com_error(exc, application=application)
        details = com_error_details(exc)
        log = logger.exception if isinstance(mapped, OfficeComError) else logger.warning
        log(
            "office.com_error",
            application=application,
            operation=operation,
            mapped=type(mapped).__name__,
            hresult=format_hresult(details.hresult),
            scode=format_hresult(details.scode),
            source=details.source,
            description=details.description,
        )
        raise mapped from exc
