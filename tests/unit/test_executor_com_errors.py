from __future__ import annotations

import pytest
import structlog
from structlog.processors import format_exc_info

from apps.executor.com import (
    CO_E_CLASSSTRING,
    DISP_E_EXCEPTION,
    E_ACCESSDENIED,
    HRESULT_LOCK_VIOLATION,
    HRESULT_SHARING_VIOLATION,
    REGDB_E_CLASSNOTREG,
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    STG_E_ACCESSDENIED,
    STG_E_LOCKVIOLATION,
    STG_E_SHAREVIOLATION,
    VBA_E_IGNORE,
    com_error_details,
    map_com_error,
    translate_com_errors,
)
from libs.core.exceptions import (
    OfficeBusyError,
    OfficeComError,
    OfficeError,
    OfficeFileLockedError,
    OfficeNotInstalledError,
)

EXCEL_GENERIC_ERROR = 0x800A03EC
SECRET_PATH = "C:\\Users\\ivan\\Документы\\Зарплаты за август.xlsx"


def signed(code: int) -> int:
    return code - 2**32 if code >= 2**31 else code


class FakeComError(Exception):
    def __init__(
        self,
        hresult: int,
        strerror: str = "Exception occurred.",
        excepinfo: tuple[object, ...] | None = None,
        argerror: int | None = None,
    ) -> None:
        super().__init__(hresult, strerror, excepinfo, argerror)
        self.hresult = hresult
        self.strerror = strerror
        self.excepinfo = excepinfo
        self.argerror = argerror


class ArgsOnlyComError(Exception):
    pass


def excepinfo(scode: int, description: str = SECRET_PATH) -> tuple[object, ...]:
    return (0, "Microsoft Excel", description, None, 0, signed(scode))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (REGDB_E_CLASSNOTREG, OfficeNotInstalledError),
        (CO_E_CLASSSTRING, OfficeNotInstalledError),
        (RPC_E_CALL_REJECTED, OfficeBusyError),
        (RPC_E_SERVERCALL_RETRYLATER, OfficeBusyError),
        (VBA_E_IGNORE, OfficeBusyError),
        (E_ACCESSDENIED, OfficeFileLockedError),
        (HRESULT_SHARING_VIOLATION, OfficeFileLockedError),
        (HRESULT_LOCK_VIOLATION, OfficeFileLockedError),
        (STG_E_ACCESSDENIED, OfficeFileLockedError),
        (STG_E_SHAREVIOLATION, OfficeFileLockedError),
        (STG_E_LOCKVIOLATION, OfficeFileLockedError),
        (EXCEL_GENERIC_ERROR, OfficeComError),
        (0x80004005, OfficeComError),
    ],
)
def test_a_top_level_hresult_maps_to_its_exception(code: int, expected: type[OfficeError]) -> None:
    mapped = map_com_error(FakeComError(signed(code)), application="Excel")

    assert type(mapped) is expected
    assert mapped.hresult == code


@pytest.mark.parametrize(
    ("scode", "expected"),
    [
        (RPC_E_CALL_REJECTED, OfficeBusyError),
        (VBA_E_IGNORE, OfficeBusyError),
        (HRESULT_SHARING_VIOLATION, OfficeFileLockedError),
        (REGDB_E_CLASSNOTREG, OfficeNotInstalledError),
        (EXCEL_GENERIC_ERROR, OfficeComError),
    ],
)
def test_disp_e_exception_is_mapped_by_the_scode_inside(
    scode: int, expected: type[OfficeError]
) -> None:
    error = FakeComError(signed(DISP_E_EXCEPTION), excepinfo=excepinfo(scode))

    mapped = map_com_error(error, application="Excel")

    assert type(mapped) is expected
    assert mapped.hresult == scode


def test_disp_e_exception_without_a_scode_is_a_generic_failure() -> None:
    error = FakeComError(signed(DISP_E_EXCEPTION), excepinfo=(0, "Excel", "boom", None, 0, 0))

    mapped = map_com_error(error, application="Excel")

    assert type(mapped) is OfficeComError
    assert mapped.hresult == DISP_E_EXCEPTION


def test_hresults_are_read_from_args_when_attributes_are_absent() -> None:
    error = ArgsOnlyComError(
        signed(DISP_E_EXCEPTION), "Exception occurred.", excepinfo(RPC_E_CALL_REJECTED), None
    )

    details = com_error_details(error)

    assert details.hresult == DISP_E_EXCEPTION
    assert details.scode == RPC_E_CALL_REJECTED
    assert details.source == "Microsoft Excel"
    assert details.description == SECRET_PATH
    assert type(map_com_error(error, application="Excel")) is OfficeBusyError


def test_an_error_without_any_hresult_is_a_generic_failure() -> None:
    mapped = map_com_error(ArgsOnlyComError("no code"), application="Word")

    assert type(mapped) is OfficeComError
    assert mapped.hresult is None
    assert "Word" in str(mapped)


@pytest.mark.parametrize(
    "code",
    [EXCEL_GENERIC_ERROR, HRESULT_SHARING_VIOLATION, RPC_E_CALL_REJECTED, REGDB_E_CLASSNOTREG],
)
def test_the_message_never_carries_the_com_error_text(code: int) -> None:
    error = FakeComError(signed(DISP_E_EXCEPTION), strerror=SECRET_PATH, excepinfo=excepinfo(code))

    mapped = map_com_error(error, application="Excel")

    assert "ivan" not in str(mapped)
    assert "Зарплаты" not in str(mapped)
    assert "ivan" not in mapped.message


def test_the_application_name_is_part_of_the_message() -> None:
    mapped = map_com_error(FakeComError(signed(RPC_E_CALL_REJECTED)), application="Word")

    assert "Word" in str(mapped)


def test_translation_raises_the_mapped_error_and_keeps_the_cause() -> None:
    original = FakeComError(signed(DISP_E_EXCEPTION), excepinfo=excepinfo(HRESULT_LOCK_VIOLATION))

    with (
        pytest.raises(OfficeFileLockedError) as raised,
        translate_com_errors(FakeComError, application="Excel", operation="save"),
    ):
        raise original

    assert raised.value.__cause__ is original


def test_translation_keeps_the_detail_in_the_server_log_only() -> None:
    original = FakeComError(signed(DISP_E_EXCEPTION), excepinfo=excepinfo(EXCEL_GENERIC_ERROR))

    with (
        structlog.testing.capture_logs([format_exc_info]) as logs,
        pytest.raises(OfficeComError) as raised,
        translate_com_errors(FakeComError, application="Excel", operation="save_as"),
    ):
        raise original

    entry = next(log for log in logs if log["event"] == "office.com_error")
    assert entry["operation"] == "save_as"
    assert entry["hresult"] == "0x80020009"
    assert entry["scode"] == "0x800A03EC"
    assert entry["description"] == SECRET_PATH
    assert entry["log_level"] == "error"
    assert "ivan" not in str(raised.value)


def test_known_failures_are_logged_as_warnings() -> None:
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(OfficeBusyError),
        translate_com_errors(FakeComError, application="Excel", operation="write_range"),
    ):
        raise FakeComError(signed(RPC_E_CALL_REJECTED))

    assert [log["log_level"] for log in logs if log["event"] == "office.com_error"] == ["warning"]


def test_other_exceptions_pass_through_untouched() -> None:
    with (
        pytest.raises(KeyError),
        translate_com_errors(FakeComError, application="Excel", operation="x"),
    ):
        raise KeyError("bug")

    with (
        pytest.raises(OfficeFileLockedError, match="свой текст"),
        translate_com_errors(FakeComError, application="Excel", operation="x"),
    ):
        raise OfficeFileLockedError("свой текст")
