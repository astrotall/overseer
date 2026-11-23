from __future__ import annotations

import ast
import inspect
import uuid
from collections.abc import AsyncIterator

import pytest
from structlog.testing import capture_logs

from apps.executor import dispatch as dispatch_module
from apps.executor import main as executor_main
from apps.executor.config import REDIS_URL_MISSING_TEXT, ExecutorSettings
from apps.executor.dispatch import ExecutorDispatcher
from libs.core.exceptions import ConfigurationError
from libs.schemas.executor import OPERATIONS, ExecutorRequest
from tests.executor_support import (
    REPORT,
    FakeExcelBackend,
    excel_dispatcher,
    open_request,
    save_request,
    write_request,
)

PASSWORD = "0123456789abcdef0123456789abcdef"


@pytest.fixture
async def dispatcher() -> AsyncIterator[tuple[ExecutorDispatcher, FakeExcelBackend]]:
    backend = FakeExcelBackend()
    built, _ = excel_dispatcher(backend)
    await built.start()
    yield built, backend
    await built.aclose()


async def test_each_request_type_reaches_its_own_handler(
    dispatcher: tuple[ExecutorDispatcher, FakeExcelBackend],
) -> None:
    executor, backend = dispatcher
    conversation = uuid.uuid4()

    opened = await executor.dispatch(open_request(REPORT, conversation_id=conversation))
    written = await executor.dispatch(write_request(conversation_id=conversation))
    saved = await executor.dispatch(save_request(None, conversation_id=conversation))

    assert opened == {"path": REPORT, "created": True}
    assert written == {
        "sheet": "Лист1",
        "address": "A1:B1",
        "rows": 1,
        "columns": 2,
        "sheet_created": False,
    }
    assert saved == {"path": REPORT, "saved_as": True}
    assert [
        name for name in backend.names if name in {"create_workbook", "write_range", "save_as"}
    ] == [
        "create_workbook",
        "write_range",
        "save_as",
    ]


async def test_anything_outside_the_request_union_is_never_dispatched(
    dispatcher: tuple[ExecutorDispatcher, FakeExcelBackend],
) -> None:
    executor, backend = dispatcher
    before = list(backend.names)

    class Impostor:
        operation = "excel.write_range"

    with pytest.raises(AssertionError):
        await executor.dispatch(Impostor())  # type: ignore[arg-type]  # нарочно чужой тип

    assert backend.names == before


def test_the_dispatcher_advertises_exactly_the_contract_operations() -> None:
    executor, _ = excel_dispatcher(FakeExcelBackend())

    assert executor.operations == OPERATIONS


def test_dispatch_never_resolves_handlers_by_name() -> None:
    tree = ast.parse(inspect.getsource(dispatch_module))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert not {"getattr", "eval", "exec", "__import__", "globals"} & names
    assert any(isinstance(node, ast.Match) for node in ast.walk(tree))


async def test_the_executor_refuses_to_start_without_a_redis_url() -> None:
    with pytest.raises(ConfigurationError) as caught:
        await executor_main.run(ExecutorSettings())

    assert str(caught.value) == REDIS_URL_MISSING_TEXT


async def test_an_unreachable_redis_is_reported_without_the_password() -> None:
    settings = ExecutorSettings(redis_url=f"redis://overseer-executor:{PASSWORD}@127.0.0.1:1/0")

    with pytest.raises(ConfigurationError) as caught:
        await executor_main.run(settings)

    assert "127.0.0.1:1" in str(caught.value)
    assert "overseer-executor" in str(caught.value)
    assert PASSWORD not in str(caught.value)


def test_main_exits_with_a_logged_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_main, "configure_logging", lambda **_: None)

    with capture_logs() as events, pytest.raises(SystemExit) as caught:
        executor_main.main()

    assert caught.value.code == 1
    assert events[-1]["event"] == "executor.stopped"
    assert events[-1]["reason"] == REDIS_URL_MISSING_TEXT


def test_dispatch_types_match_the_request_union() -> None:
    hints = inspect.get_annotations(ExecutorDispatcher.dispatch, eval_str=True)

    assert hints["request"] == ExecutorRequest
