from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from apps.executor.dispatch import ExcelOperations, ExecutorDispatcher
from apps.executor.excel.backend import WorkbookHandle
from apps.executor.excel.controller import ExcelController
from apps.executor.excel.sessions import ConversationWorkbooks
from apps.executor.excel.validation import CellRows
from apps.executor.paths import PathPolicy
from libs.executor.keys import NAMESPACE
from libs.schemas.executor import (
    OPERATIONS,
    ExecutorHeartbeat,
    OpenOrCreateWorkbookArguments,
    OpenOrCreateWorkbookRequest,
    SaveWorkbookArguments,
    SaveWorkbookRequest,
    WriteRangeArguments,
    WriteRangeRequest,
    encode_heartbeat,
)
from libs.schemas.executor import CellValue as WireCellValue

ALLOWED_ROOT = "C:\\Users\\me\\Documents\\Overseer"
REPORT = f"{ALLOWED_ROOT}\\report.xlsx"
WAIT_S = 2.0


def window(budget_s: float) -> tuple[datetime, datetime]:
    issued_at = datetime.now(UTC)
    return issued_at, issued_at + timedelta(seconds=budget_s)


def write_request(
    *,
    conversation_id: uuid.UUID | None = None,
    budget_s: float = 10.0,
    sheet: str = "Лист1",
    data: list[list[WireCellValue]] | None = None,
) -> WriteRangeRequest:
    issued_at, deadline = window(budget_s)
    return WriteRangeRequest(
        id=uuid.uuid4(),
        conversation_id=conversation_id or uuid.uuid4(),
        issued_at=issued_at,
        deadline=deadline,
        arguments=WriteRangeArguments(sheet=sheet, start_cell="A1", data=data or [["x", 1]]),
    )


def open_request(
    path: str | None, *, conversation_id: uuid.UUID, budget_s: float = 10.0
) -> OpenOrCreateWorkbookRequest:
    issued_at, deadline = window(budget_s)
    return OpenOrCreateWorkbookRequest(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        issued_at=issued_at,
        deadline=deadline,
        arguments=OpenOrCreateWorkbookArguments(path=path),
    )


def save_request(
    path: str | None,
    *,
    conversation_id: uuid.UUID,
    overwrite: bool = False,
    budget_s: float = 10.0,
) -> SaveWorkbookRequest:
    issued_at, deadline = window(budget_s)
    return SaveWorkbookRequest(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        issued_at=issued_at,
        deadline=deadline,
        arguments=SaveWorkbookArguments(path=path, overwrite=overwrite),
    )


def heartbeat(instance_id: uuid.UUID, *, operations: Iterable[str] = OPERATIONS) -> str:
    now = datetime.now(UTC)
    return encode_heartbeat(
        ExecutorHeartbeat(
            instance_id=instance_id,
            started_at=now,
            sent_at=now,
            operations=sorted(operations),
        )
    )


async def list_length(redis: Redis, key: str) -> int:
    return int(await redis.execute_command("LLEN", key))


async def purge_executor_keys(redis: Redis) -> None:
    keys = [key async for key in redis.scan_iter(match=f"{NAMESPACE}*")]
    if keys:
        await redis.delete(*keys)


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


def excel_dispatcher(
    backend: FakeExcelBackend,
    *,
    roots: Iterable[str] = (ALLOWED_ROOT,),
    max_conversations: int = 4,
    idle_ttl_s: float = 600.0,
    monotonic: Callable[[], float] | None = None,
) -> tuple[ExecutorDispatcher, ConversationWorkbooks]:
    controller = ExcelController(backend, call_timeout_s=WAIT_S, stop_timeout_s=WAIT_S)
    workbooks = ConversationWorkbooks(
        controller,
        max_conversations=max_conversations,
        idle_ttl_s=idle_ttl_s,
        sweep_interval_s=0.0,
        **({} if monotonic is None else {"monotonic": monotonic}),
    )
    paths = PathPolicy(list(roots))
    return ExecutorDispatcher(excel=ExcelOperations(workbooks, paths=paths)), workbooks
