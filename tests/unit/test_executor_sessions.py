from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from structlog.testing import capture_logs

from apps.executor.dispatch import ExecutorDispatcher
from apps.executor.excel.sessions import ConversationWorkbooks
from libs.core.exceptions import OfficeBusyError, OfficeFileLockedError, OfficeInputError
from tests.executor_support import (
    ALLOWED_ROOT,
    FakeExcelBackend,
    excel_dispatcher,
    open_request,
    save_request,
    write_request,
)

IDLE_TTL_S = 60.0
FIRST = f"{ALLOWED_ROOT}\\first.xlsx"
SECOND = f"{ALLOWED_ROOT}\\second.xlsx"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Harness:
    dispatcher: ExecutorDispatcher
    workbooks: ConversationWorkbooks
    backend: FakeExcelBackend
    clock: Clock


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    backend, clock = FakeExcelBackend(), Clock()
    dispatcher, workbooks = excel_dispatcher(
        backend, max_conversations=2, idle_ttl_s=IDLE_TTL_S, monotonic=clock
    )
    await dispatcher.start()
    yield Harness(dispatcher, workbooks, backend, clock)
    await dispatcher.aclose()


async def open_in(harness: Harness, conversation: uuid.UUID, path: str | None) -> None:
    await harness.dispatcher.dispatch(open_request(path, conversation_id=conversation))


async def write_in(harness: Harness, conversation: uuid.UUID, value: str) -> None:
    await harness.dispatcher.dispatch(write_request(conversation_id=conversation, data=[[value]]))


async def test_two_conversations_write_into_their_own_workbooks(harness: Harness) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()

    await open_in(harness, alice, FIRST)
    await open_in(harness, bob, SECOND)
    await write_in(harness, alice, "alice")
    await write_in(harness, bob, "bob")
    await write_in(harness, alice, "alice again")

    writes = [book.writes for book in harness.backend.workbooks.values()]
    assert [[str(values[0][0]) for _, _, values in book] for book in writes] == [
        ["'alice", "'alice again"],
        ["'bob"],
    ]


async def test_a_conversation_without_a_workbook_does_not_see_another_ones(
    harness: Harness,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    await open_in(harness, alice, FIRST)

    with pytest.raises(OfficeInputError):
        await write_in(harness, bob, "bob")
    with pytest.raises(OfficeInputError):
        await harness.dispatcher.dispatch(save_request(None, conversation_id=bob))

    assert all(not book.writes for book in harness.backend.workbooks.values())


async def test_a_file_already_open_in_another_conversation_is_refused(
    harness: Harness,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    await open_in(harness, alice, FIRST)

    with pytest.raises(OfficeFileLockedError):
        await open_in(harness, bob, FIRST)
    with pytest.raises(OfficeFileLockedError):
        await open_in(harness, bob, f"{ALLOWED_ROOT}\\sub\\FIRST.xlsx")
    await open_in(harness, bob, None)
    with pytest.raises(OfficeFileLockedError):
        await harness.dispatcher.dispatch(save_request(FIRST, conversation_id=bob))

    assert harness.workbooks.open_conversations == {alice, bob}


async def test_an_idle_clean_workbook_is_closed_after_the_ttl(harness: Harness) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    await open_in(harness, alice, FIRST)
    harness.clock.now += IDLE_TTL_S / 2
    await open_in(harness, bob, SECOND)
    harness.clock.now += IDLE_TTL_S / 2 + 1

    await harness.dispatcher.idle()

    assert harness.workbooks.open_conversations == {bob}
    assert sum(not book.closed for book in harness.backend.workbooks.values()) == 1


async def test_an_idle_workbook_with_unsaved_changes_is_kept_and_reported_once(
    harness: Harness,
) -> None:
    alice = uuid.uuid4()
    await open_in(harness, alice, FIRST)
    await write_in(harness, alice, "draft")
    harness.clock.now += IDLE_TTL_S + 1

    with capture_logs() as events:
        await harness.dispatcher.idle()
        await harness.dispatcher.idle()

    assert harness.workbooks.open_conversations == {alice}
    assert "save" not in harness.backend.names
    assert "save_as" not in harness.backend.names
    kept = [event for event in events if event["event"] == "excel.unsaved_workbook_kept"]
    assert len(kept) == 1
    assert kept[0]["conversation_id"] == str(alice)


async def test_a_kept_workbook_is_closed_once_its_conversation_saves_it(
    harness: Harness,
) -> None:
    alice = uuid.uuid4()
    await open_in(harness, alice, FIRST)
    await write_in(harness, alice, "draft")
    harness.clock.now += IDLE_TTL_S + 1
    await harness.dispatcher.idle()

    await harness.dispatcher.dispatch(save_request(None, conversation_id=alice))
    harness.clock.now += IDLE_TTL_S + 1
    await harness.dispatcher.idle()

    assert harness.workbooks.open_conversations == frozenset()


async def test_the_cap_evicts_the_least_recently_used_clean_workbook(harness: Harness) -> None:
    alice, bob, carol = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await open_in(harness, alice, FIRST)
    harness.clock.now += 1
    await open_in(harness, bob, SECOND)
    harness.clock.now += 1
    await write_in(harness, alice, "touch")
    await harness.dispatcher.dispatch(save_request(None, conversation_id=alice))
    harness.clock.now += 1

    await open_in(harness, carol, None)

    assert harness.workbooks.open_conversations == {alice, carol}


async def test_the_cap_never_evicts_unsaved_work_and_refuses_the_newcomer(
    harness: Harness,
) -> None:
    alice, bob, carol = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for conversation, path in ((alice, FIRST), (bob, SECOND)):
        await open_in(harness, conversation, path)
        await write_in(harness, conversation, "draft")

    with pytest.raises(OfficeBusyError):
        await open_in(harness, carol, None)

    assert harness.workbooks.open_conversations == {alice, bob}
    assert all(not book.closed for book in harness.backend.workbooks.values())
    assert "save" not in harness.backend.names
    assert "save_as" not in harness.backend.names


async def test_a_conversation_already_holding_a_workbook_is_not_counted_against_the_cap(
    harness: Harness,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    for conversation, path in ((alice, FIRST), (bob, SECOND)):
        await open_in(harness, conversation, path)
        await write_in(harness, conversation, "draft")
    await harness.dispatcher.dispatch(save_request(None, conversation_id=alice))

    await open_in(harness, alice, f"{ALLOWED_ROOT}\\third.xlsx")

    assert harness.workbooks.open_conversations == {alice, bob}


async def test_a_released_workbook_has_to_be_opened_again(harness: Harness) -> None:
    alice = uuid.uuid4()
    await open_in(harness, alice, FIRST)
    harness.clock.now += IDLE_TTL_S + 1
    await harness.dispatcher.idle()

    with pytest.raises(OfficeInputError):
        await write_in(harness, alice, "late")

    await open_in(harness, alice, FIRST)
    await write_in(harness, alice, "reopened")
