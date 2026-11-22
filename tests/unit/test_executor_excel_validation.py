from __future__ import annotations

from decimal import Decimal

import pytest

from apps.executor.excel.validation import (
    MAX_CELL_TEXT_CHARS,
    MAX_COLUMNS,
    MAX_EXACT_INTEGER,
    MAX_PATH_CHARS,
    MAX_ROWS,
    MAX_WRITE_CELLS,
    TEXT_PREFIX,
    XL_EXCEL8,
    XL_EXCEL12,
    XL_OPEN_XML_WORKBOOK,
    XL_OPEN_XML_WORKBOOK_MACRO_ENABLED,
    CellRef,
    CellValue,
    cell_payload,
    column_index,
    column_letters,
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
from libs.core.exceptions import OfficeInputError


@pytest.mark.parametrize(
    ("letters", "index"),
    [("A", 1), ("Z", 26), ("AA", 27), ("AZ", 52), ("BA", 53), ("ZZ", 702), ("AAA", 703)],
)
def test_column_letters_round_trip(letters: str, index: int) -> None:
    assert column_index(letters) == index
    assert column_letters(index) == letters


def test_the_last_column_is_xfd() -> None:
    assert column_index("XFD") == MAX_COLUMNS
    assert column_letters(MAX_COLUMNS) == "XFD"


@pytest.mark.parametrize("index", [0, -1, MAX_COLUMNS + 1])
def test_a_column_number_off_the_sheet_is_rejected(index: int) -> None:
    with pytest.raises(OfficeInputError):
        column_letters(index)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A1", CellRef(row=1, column=1)),
        ("AA1", CellRef(row=1, column=27)),
        ("ab12", CellRef(row=12, column=28)),
        ("$C$7", CellRef(row=7, column=3)),
        ("  B2 ", CellRef(row=2, column=2)),
        (f"XFD{MAX_ROWS}", CellRef(row=MAX_ROWS, column=MAX_COLUMNS)),
    ],
)
def test_a1_cells_are_parsed(text: str, expected: CellRef) -> None:
    assert parse_cell(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "A", "1", "1A", "A0", "A01x", "XFE1", f"A{MAX_ROWS + 1}", "AAAA1", "A1:B2", "R1C1", "Ä1"],
)
def test_anything_but_a_single_a1_cell_is_rejected(text: str) -> None:
    with pytest.raises(OfficeInputError):
        parse_cell(text)


def test_a_huge_cell_reference_is_not_echoed_back_in_full() -> None:
    with pytest.raises(OfficeInputError) as raised:
        parse_cell("A" * 500)

    assert len(str(raised.value)) < 200


@pytest.mark.parametrize(
    ("start", "rows", "columns", "address"),
    [
        ("A1", 1, 1, "A1"),
        ("A1", 2, 3, "A1:C2"),
        ("Z5", 1, 2, "Z5:AA5"),
        ("AA1", 3, 1, "AA1:AA3"),
        (f"XFD{MAX_ROWS}", 1, 1, f"XFD{MAX_ROWS}"),
    ],
)
def test_the_range_address_covers_the_data(
    start: str, rows: int, columns: int, address: str
) -> None:
    assert range_address(parse_cell(start), rows, columns) == address


@pytest.mark.parametrize(
    ("start", "rows", "columns"),
    [(f"A{MAX_ROWS}", 2, 1), ("XFD1", 1, 2), ("A1", 0, 1), ("A1", 1, 0)],
)
def test_a_range_that_falls_off_the_sheet_is_rejected(start: str, rows: int, columns: int) -> None:
    with pytest.raises(OfficeInputError):
        range_address(parse_cell(start), rows, columns)


@pytest.mark.parametrize("name", ["Sheet1", "Лист1", "Отчёт за август", "a" * 31, "it's"])
def test_valid_sheet_names_pass(name: str) -> None:
    assert validate_sheet_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "a" * 32, "'quoted", "quoted'", "History", "history"]
    + [f"bad{char}name" for char in ":\\/?*[]"],
)
def test_invalid_sheet_names_are_rejected(name: str) -> None:
    with pytest.raises(OfficeInputError):
        validate_sheet_name(name)


def test_sheets_match_case_insensitively_and_keep_the_existing_spelling() -> None:
    existing = ["Лист1", "Итоги"]

    assert match_sheet(existing, "лист1") == "Лист1"
    assert match_sheet(existing, "ИТОГИ") == "Итоги"
    assert match_sheet(existing, "Отчёт") is None


def test_rectangular_rows_of_supported_types_pass_and_become_tuples() -> None:
    rows = validate_rows([["имя", 1, 2.5], [True, None, ""]])

    assert rows == (("имя", 1, 2.5), (True, None, ""))


@pytest.mark.parametrize(
    "data",
    [
        [],
        (),
        "abc",
        {"a": 1},
        None,
        [[]],
        [["a"], []],
        ["abc"],
        [["a", "b"], ["c"]],
        [["a"], ["b", "c"]],
    ],
)
def test_misshapen_data_is_rejected(data: object) -> None:
    with pytest.raises(OfficeInputError):
        validate_rows(data)


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},
        ["nested"],
        b"bytes",
        Decimal("1.5"),
        float("nan"),
        float("inf"),
        float("-inf"),
        MAX_EXACT_INTEGER + 1,
        -(MAX_EXACT_INTEGER + 1),
        "x" * (MAX_CELL_TEXT_CHARS + 1),
        object(),
    ],
)
def test_unsupported_cell_values_are_rejected(value: object) -> None:
    with pytest.raises(OfficeInputError) as raised:
        validate_rows([["ok", value]])

    assert "столбец 2" in str(raised.value)


def test_boundary_values_are_accepted() -> None:
    rows = validate_rows([[MAX_EXACT_INTEGER, -MAX_EXACT_INTEGER, "x" * MAX_CELL_TEXT_CHARS]])

    assert rows[0][0] == MAX_EXACT_INTEGER


def test_the_cell_cap_is_inclusive() -> None:
    assert len(validate_rows([[0] * 100] * (MAX_WRITE_CELLS // 100))) == MAX_WRITE_CELLS // 100


@pytest.mark.parametrize(
    "data",
    [
        [[0] * 101] * (MAX_WRITE_CELLS // 100),
        [[0]] * (MAX_WRITE_CELLS + 1),
        [[0] * (MAX_WRITE_CELLS + 1)],
    ],
)
def test_more_cells_than_the_cap_are_rejected(data: list[list[int]]) -> None:
    with pytest.raises(OfficeInputError) as raised:
        validate_rows(data)

    assert str(MAX_WRITE_CELLS) in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "=1+1",
        '=HYPERLINK("http://evil.example","click")',
        "+1+1",
        "-1+1",
        "@SUM(A1:A2)",
        "=cmd|' /C calc'!A0",
        "00123",
        "plain text",
        "'already quoted",
    ],
)
def test_strings_are_written_as_literal_text_by_default(text: str) -> None:
    assert cell_payload(text, formulas=False) == TEXT_PREFIX + text


@pytest.mark.parametrize("value", [None, "", 0, -5, 2.5, True, False])
def test_non_text_and_empty_values_are_written_as_is(value: CellValue) -> None:
    assert cell_payload(value, formulas=False) == value


def test_the_formula_opt_in_passes_only_equals_formulas_through() -> None:
    assert cell_payload("=SUM(A1:A3)", formulas=True) == "=SUM(A1:A3)"
    assert cell_payload("+1+1", formulas=True) == TEXT_PREFIX + "+1+1"
    assert cell_payload("-1", formulas=True) == TEXT_PREFIX + "-1"
    assert cell_payload("@SUM(A1)", formulas=True) == TEXT_PREFIX + "@SUM(A1)"
    assert cell_payload("Итого", formulas=True) == TEXT_PREFIX + "Итого"


def test_the_payload_keeps_the_shape() -> None:
    rows = validate_rows([["=1+1", 1], [None, "x"]])

    assert to_excel_payload(rows, formulas=False) == (("'=1+1", 1), (None, "'x"))


@pytest.mark.parametrize(
    ("path", "normalized"),
    [
        ("C:\\Users\\me\\отчёт.xlsx", "C:\\Users\\me\\отчёт.xlsx"),
        ("C:/Users/me/report.XLSM", "C:\\Users\\me\\report.XLSM"),
        ("\\\\server\\share\\report.xlsb", "\\\\server\\share\\report.xlsb"),
        ("D:\\old.xls", "D:\\old.xls"),
    ],
)
def test_absolute_workbook_paths_are_normalized(path: str, normalized: str) -> None:
    assert validate_workbook_path(path) == normalized


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "report.xlsx",
        "..\\report.xlsx",
        "C:report.xlsx",
        "\\report.xlsx",
        "/tmp/report.xlsx",
        "C:\\report.csv",
        "C:\\report",
        "C:\\re\x00port.xlsx",
        "C:\\" + "a" * MAX_PATH_CHARS + ".xlsx",
    ],
)
def test_invalid_workbook_paths_are_rejected(path: str) -> None:
    with pytest.raises(OfficeInputError):
        validate_workbook_path(path)


def test_a_rejected_path_is_not_echoed_back() -> None:
    with pytest.raises(OfficeInputError) as raised:
        validate_workbook_path("C:\\Users\\ivan\\Зарплаты.csv")

    assert "ivan" not in str(raised.value)


@pytest.mark.parametrize(
    ("path", "file_format"),
    [
        ("C:\\a.xlsx", XL_OPEN_XML_WORKBOOK),
        ("C:\\a.XLSM", XL_OPEN_XML_WORKBOOK_MACRO_ENABLED),
        ("C:\\a.xlsb", XL_EXCEL12),
        ("C:\\a.xls", XL_EXCEL8),
    ],
)
def test_the_save_format_follows_the_extension(path: str, file_format: int) -> None:
    assert workbook_format(path) == file_format


def test_windows_paths_compare_case_insensitively() -> None:
    assert same_path("C:\\Users\\Me\\A.xlsx", "c:/users/me/a.XLSX")
    assert not same_path("C:\\a.xlsx", "C:\\b.xlsx")
