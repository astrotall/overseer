from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.executor.sta import (
    StaThread,
    StaThreadUnavailableError,
    ThreadAffinity,
    _deliver,
)
from libs.core.exceptions import ConfigurationError

WAIT_S = 2.0


class Recorder:
    def __init__(self, *, fail_on_init: BaseException | None = None) -> None:
        self.fail_on_init = fail_on_init
        self.events: list[tuple[str, int]] = []
        self.idle_threads: set[int] = set()

    def initialize(self) -> None:
        self.events.append(("initialize", threading.get_ident()))
        if self.fail_on_init is not None:
            raise self.fail_on_init

    def finalize(self) -> None:
        self.events.append(("finalize", threading.get_ident()))

    def idle(self) -> None:
        self.idle_threads.add(threading.get_ident())

    def thread(self, **kwargs: Any) -> StaThread:
        return StaThread(
            name="test-sta",
            initialize=self.initialize,
            finalize=self.finalize,
            idle=self.idle,
            poll_interval_s=0.01,
            **kwargs,
        )


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, Any]]]:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    yield errors
    loop.set_exception_handler(previous)


async def _stop(thread: StaThread, join_s: float = WAIT_S) -> bool:
    return await asyncio.to_thread(thread.stop, join_s)


async def test_every_call_runs_on_one_dedicated_thread_that_is_not_the_loop_thread() -> None:
    recorder = Recorder()
    sta = recorder.thread()
    sta.start()

    idents = [await sta.call(threading.get_ident) for _ in range(5)]
    idents += await asyncio.gather(*(sta.call(threading.get_ident) for _ in range(5)))
    await _stop(sta)

    loop_thread = threading.get_ident()
    assert len(set(idents)) == 1
    assert idents[0] != loop_thread
    assert recorder.events == [("initialize", idents[0]), ("finalize", idents[0])]


async def test_the_idle_hook_runs_on_the_same_thread() -> None:
    recorder = Recorder()
    sta = recorder.thread()
    sta.start()

    owner = await sta.call(threading.get_ident)
    await asyncio.sleep(0.1)
    await _stop(sta)

    assert recorder.idle_threads == {owner}


async def test_calling_before_start_is_refused() -> None:
    sta = Recorder().thread()

    with pytest.raises(StaThreadUnavailableError):
        await sta.call(threading.get_ident)


async def test_a_failed_initialization_surfaces_on_the_first_call_and_skips_finalize() -> None:
    failure = ConfigurationError("COM недоступен")
    recorder = Recorder(fail_on_init=failure)
    sta = recorder.thread()
    sta.start()

    with pytest.raises(ConfigurationError) as raised:
        await asyncio.wait_for(sta.call(threading.get_ident), WAIT_S)
    await asyncio.to_thread(_wait_until, lambda: not sta.running)

    assert raised.value is failure
    assert [name for name, _ in recorder.events] == ["initialize"]
    with pytest.raises(ConfigurationError):
        await sta.call(threading.get_ident)
    assert await _stop(sta)


async def test_an_exception_inside_the_call_reaches_the_caller() -> None:
    sta = Recorder().thread()
    sta.start()

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await sta.call(boom)
    assert await sta.call(lambda: 42) == 42
    await _stop(sta)


async def test_stop_resolves_a_running_call_and_every_queued_one(
    loop_errors: list[dict[str, Any]],
) -> None:
    sta = Recorder().thread()
    sta.start()
    entered = threading.Event()
    release = threading.Event()

    def stuck() -> str:
        entered.set()
        release.wait(WAIT_S * 5)
        return "late"

    running = asyncio.ensure_future(sta.call(stuck))
    queued = [asyncio.ensure_future(sta.call(threading.get_ident)) for _ in range(3)]
    await asyncio.to_thread(entered.wait, WAIT_S)

    stopped = await _stop(sta, join_s=0.1)
    results = await asyncio.wait_for(
        asyncio.gather(running, *queued, return_exceptions=True), WAIT_S
    )

    assert stopped is False
    assert all(isinstance(result, StaThreadUnavailableError) for result in results)
    assert sta.running

    release.set()
    await asyncio.to_thread(_wait_until, lambda: not sta.running)
    await asyncio.sleep(0.05)
    assert loop_errors == []


async def test_a_stop_that_timed_out_keeps_the_thread_and_refuses_a_second_one() -> None:
    sta = Recorder().thread()
    sta.start()
    entered = threading.Event()
    release = threading.Event()

    def stuck() -> None:
        entered.set()
        release.wait(WAIT_S * 5)

    pending = asyncio.ensure_future(sta.call(stuck))
    await asyncio.to_thread(entered.wait, WAIT_S)
    assert await _stop(sta, join_s=0.05) is False

    with pytest.raises(RuntimeError, match="still running"):
        sta.start()
    with pytest.raises(StaThreadUnavailableError):
        await sta.call(threading.get_ident)

    release.set()
    await asyncio.to_thread(_wait_until, lambda: not sta.running)
    sta.start()
    assert await sta.call(lambda: "again") == "again"
    await _stop(sta)
    await asyncio.gather(pending, return_exceptions=True)


async def test_a_restart_gets_a_fresh_thread_and_a_fresh_apartment() -> None:
    recorder = Recorder()
    sta = recorder.thread()

    sta.start()
    first = await sta.call(threading.get_ident)
    assert await _stop(sta)
    with pytest.raises(StaThreadUnavailableError):
        await sta.call(threading.get_ident)

    sta.start()
    second = await sta.call(threading.get_ident)
    await _stop(sta)

    assert [name for name, _ in recorder.events] == [
        "initialize",
        "finalize",
        "initialize",
        "finalize",
    ]
    assert recorder.events[0][1] == first
    assert recorder.events[2][1] == second


async def test_a_caller_that_gave_up_waiting_does_not_leave_its_call_queued() -> None:
    sta = Recorder().thread()
    sta.start()
    entered = threading.Event()
    release = threading.Event()
    skipped_ran = threading.Event()

    def stuck() -> None:
        entered.set()
        release.wait(WAIT_S * 5)

    blocker = asyncio.ensure_future(sta.call(stuck))
    await asyncio.to_thread(entered.wait, WAIT_S)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await sta.call(skipped_ran.set)

    release.set()
    await blocker
    assert await sta.call(lambda: "next") == "next"
    await _stop(sta)

    assert not skipped_ran.is_set()


async def test_a_cancelled_caller_does_not_leave_its_call_queued() -> None:
    sta = Recorder().thread()
    sta.start()
    entered = threading.Event()
    release = threading.Event()
    skipped_ran = threading.Event()

    def stuck() -> None:
        entered.set()
        release.wait(WAIT_S * 5)

    blocker = asyncio.ensure_future(sta.call(stuck))
    await asyncio.to_thread(entered.wait, WAIT_S)
    victim = asyncio.ensure_future(sta.call(skipped_ran.set))
    await asyncio.sleep(0)
    victim.cancel()
    await asyncio.gather(victim, return_exceptions=True)

    release.set()
    await blocker
    await sta.call(lambda: None)
    await _stop(sta)

    assert not skipped_ran.is_set()


async def test_two_concurrent_starts_leave_exactly_one_thread() -> None:
    sta = Recorder().thread()
    barrier = threading.Barrier(2)
    outcomes: list[BaseException | None] = []

    def start() -> None:
        barrier.wait()
        try:
            sta.start()
        except RuntimeError as exc:
            outcomes.append(exc)
        else:
            outcomes.append(None)

    await asyncio.gather(asyncio.to_thread(start), asyncio.to_thread(start))
    await _stop(sta)

    assert sorted(outcome is None for outcome in outcomes) == [False, True]


def test_resolving_after_the_loop_closed_is_a_silent_no_op() -> None:
    loop = asyncio.new_event_loop()
    future: asyncio.Future[int] = loop.create_future()
    loop.close()

    _deliver(loop, future, 1)
    _deliver(loop, future, RuntimeError("late"))

    assert not future.done()


def test_a_double_resolve_keeps_the_first_outcome() -> None:
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[str] = loop.create_future()
        _deliver(loop, future, "first")
        _deliver(loop, future, RuntimeError("second"))
        loop.run_until_complete(asyncio.sleep(0))
        assert future.result() == "first"
    finally:
        loop.close()


def test_thread_affinity_rejects_other_threads_and_unbound_use() -> None:
    affinity = ThreadAffinity()
    with pytest.raises(RuntimeError, match="enter_thread"):
        affinity.check()

    affinity.bind()
    affinity.check()
    errors: list[BaseException] = []

    def foreign() -> None:
        try:
            affinity.check()
        except RuntimeError as exc:
            errors.append(exc)

    worker = threading.Thread(target=foreign)
    worker.start()
    worker.join()

    assert len(errors) == 1
    affinity.release()
    assert affinity.owner is None


def _wait_until(predicate: Any, timeout: float = WAIT_S) -> None:
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        deadline.wait(0.01)
    raise AssertionError("condition not reached")
