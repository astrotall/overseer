from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

from libs.core.exceptions import OfficeInputError

CellValue = str | int | float | bool | None
CellRows = tuple[tuple[CellValue, ...], ...]

MAX_ROWS: Final = 1_048_576
MAX_COLUMNS: Final = 16_384
MAX_WRITE_CELLS: Final = 10_000
MAX_CELL_TEXT_CHARS: Final = 32_767
MAX_EXACT_INTEGER: Final = 999_999_999_999_999
MAX_SHEET_NAME_CHARS: Final = 31
MAX_PATH_CHARS: Final = 218
MAX_ECHOED_CHARS: Final = 40

FORBIDDEN_SHEET_CHARS: Final = frozenset(":\\/?*[]")
RESERVED_SHEET_NAMES: Final = frozenset({"history"})

TEXT_PREFIX: Final = "'"
FORMULA_PREFIX: Final = "="

XL_OPEN_XML_WORKBOOK: Final = 51
XL_OPEN_XML_WORKBOOK_MACRO_ENABLED: Final = 52
XL_EXCEL12: Final = 50
XL_EXCEL8: Final = 56

WORKBOOK_FORMATS: Final[Mapping[str, int]] = {
    ".xlsx": XL_OPEN_XML_WORKBOOK,
    ".xlsm": XL_OPEN_XML_WORKBOOK_MACRO_ENABLED,
    ".xlsb": XL_EXCEL12,
    ".xls": XL_EXCEL8,
}

_CELL_PATTERN: Final = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")


@dataclass(frozen=True, slots=True)
class CellRef:
    row: int
    column: int

    @property
    def a1(self) -> str:
        return f"{column_letters(self.column)}{self.row}"


def _echo(text: str) -> str:
    if len(text) <= MAX_ECHOED_CHARS:
        return repr(text)
    return repr(text[:MAX_ECHOED_CHARS] + "…")


def column_index(letters: str) -> int:
    if not letters or not letters.isascii() or not letters.isalpha():
        raise OfficeInputError(f"Некорректное имя столбца {_echo(letters)}")

    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def column_letters(index: int) -> str:
    if not 1 <= index <= MAX_COLUMNS:
        raise OfficeInputError(f"Номер столбца вне листа Excel: {index}")

    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def parse_cell(text: str) -> CellRef:
    match = _CELL_PATTERN.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise OfficeInputError(
            f"Некорректный адрес ячейки {_echo(str(text))}: ожидается одна ячейка "
            "в нотации A1, например B2 или AA10"
        )

    column = column_index(match.group(1))
    row = int(match.group(2))
    if not 1 <= column <= MAX_COLUMNS or not 1 <= row <= MAX_ROWS:
        raise OfficeInputError(
            f"Ячейка {_echo(text.strip())} вне листа Excel: последняя ячейка листа — XFD{MAX_ROWS}"
        )
    return CellRef(row=row, column=column)


def range_address(start: CellRef, rows: int, columns: int) -> str:
    if rows < 1 or columns < 1:
        raise OfficeInputError("Диапазон должен содержать хотя бы одну ячейку")

    last_row = start.row + rows - 1
    last_column = start.column + columns - 1
    if last_row > MAX_ROWS or last_column > MAX_COLUMNS:
        raise OfficeInputError(
            f"Данные {rows}×{columns} от ячейки {start.a1} не помещаются на лист Excel"
        )

    end = CellRef(row=last_row, column=last_column)
    return start.a1 if end == start else f"{start.a1}:{end.a1}"


def validate_sheet_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise OfficeInputError("Имя листа не может быть пустым")
    if len(name) > MAX_SHEET_NAME_CHARS:
        raise OfficeInputError(f"Имя листа длиннее {MAX_SHEET_NAME_CHARS} символов: {_echo(name)}")

    forbidden = sorted({char for char in name if char in FORBIDDEN_SHEET_CHARS})
    if forbidden:
        raise OfficeInputError(
            f"Имя листа {_echo(name)} содержит запрещённые символы: {' '.join(forbidden)}"
        )
    if name.startswith("'") or name.endswith("'"):
        raise OfficeInputError(
            f"Имя листа {_echo(name)} не может начинаться или заканчиваться апострофом"
        )
    if name.casefold() in RESERVED_SHEET_NAMES:
        raise OfficeInputError(f"Имя листа {_echo(name)} зарезервировано Excel")
    return name


def match_sheet(existing: Sequence[str], requested: str) -> str | None:
    wanted = requested.casefold()
    for name in existing:
        if name.casefold() == wanted:
            return name
    return None


def _check_cell(value: object, row: int, column: int) -> CellValue:
    position = f"Строка {row}, столбец {column}"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_EXACT_INTEGER:
            raise OfficeInputError(
                f"{position}: целое число длиннее 15 знаков Excel хранит неточно — "
                "передайте его строкой"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OfficeInputError(f"{position}: NaN и бесконечность в ячейку не записываются")
        return value
    if isinstance(value, str):
        if len(value) > MAX_CELL_TEXT_CHARS:
            raise OfficeInputError(
                f"{position}: текст длиннее {MAX_CELL_TEXT_CHARS} символов не помещается в ячейку"
            )
        return value
    raise OfficeInputError(
        f"{position}: тип {type(value).__name__} не поддерживается — "
        "допустимы строка, число, логическое значение и пустая ячейка"
    )


def validate_rows(data: object) -> CellRows:
    if not isinstance(data, list | tuple) or not data:
        raise OfficeInputError("Данные должны быть непустым списком строк")
    if len(data) > MAX_WRITE_CELLS:
        raise OfficeInputError(
            f"Слишком много ячеек: за один вызов записывается не больше {MAX_WRITE_CELLS}"
        )

    for index, row in enumerate(data, start=1):
        if not isinstance(row, list | tuple):
            raise OfficeInputError(f"Строка {index}: ожидается список значений ячеек")
        if not row:
            raise OfficeInputError(
                f"Строка {index} пустая: в каждой строке нужна хотя бы одна ячейка"
            )
        if len(row) != len(data[0]):
            raise OfficeInputError(
                f"Данные не прямоугольные: в строке 1 {len(data[0])} ячеек, "
                f"в строке {index} — {len(row)}"
            )

    width = len(data[0])
    if len(data) * width > MAX_WRITE_CELLS:
        raise OfficeInputError(
            f"Слишком много ячеек: {len(data)}×{width}, за один вызов записывается "
            f"не больше {MAX_WRITE_CELLS}"
        )

    return tuple(
        tuple(
            _check_cell(value, row_number, column_number)
            for column_number, value in enumerate(row, start=1)
        )
        for row_number, row in enumerate(data, start=1)
    )


def cell_payload(value: CellValue, *, formulas: bool) -> CellValue:
    if not isinstance(value, str) or not value:
        return value
    if formulas and value.startswith(FORMULA_PREFIX):
        return value
    return TEXT_PREFIX + value


def to_excel_payload(rows: CellRows, *, formulas: bool) -> CellRows:
    return tuple(tuple(cell_payload(value, formulas=formulas) for value in row) for row in rows)


def validate_workbook_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise OfficeInputError("Путь к книге не может быть пустым")
    if "\x00" in path:
        raise OfficeInputError("Путь к книге содержит недопустимый символ")

    pure = PureWindowsPath(path)
    if not pure.is_absolute():
        raise OfficeInputError(
            "Путь к книге должен быть абсолютным, например C:\\Users\\<имя>\\Documents\\отчёт.xlsx"
        )
    if pure.suffix.casefold() not in WORKBOOK_FORMATS:
        raise OfficeInputError(
            f"Неподдерживаемое расширение книги: допустимы {', '.join(WORKBOOK_FORMATS)}"
        )

    normalized = str(pure)
    if len(normalized) > MAX_PATH_CHARS:
        raise OfficeInputError(
            f"Путь к книге длиннее {MAX_PATH_CHARS} символов: Excel не работает с такими путями"
        )
    return normalized


def workbook_format(path: str) -> int:
    return WORKBOOK_FORMATS[PureWindowsPath(path).suffix.casefold()]


def same_path(first: str, second: str) -> bool:
    return PureWindowsPath(first) == PureWindowsPath(second)
