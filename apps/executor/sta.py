from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable
from typing import Final, TypeVar

from libs.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

POLL_INTERVAL_S: Final = 0.05
STOP_TIMEOUT_S: Final = 15.0

NOT_RUNNING_TEXT: Final = "STA-поток не запущен: start() не вызывался или поток уже остановлен"
STOPPED_TEXT: Final = "STA-поток остановлен, вызов не выполнен или его результат утерян"


class StaThreadUnavailableError(RuntimeError):
    pass


def _noop() -> None:
    return None


class _WorkItem:
    __slots__ = ("abandon", "run", "settled")

    def __init__(self, run: Callable[[], None], abandon: Callable[[BaseException], None]) -> None:
        self.run = run
        self.abandon = abandon
        self.settled = threading.Event()


class StaThread:
    def __init__(
        self,
        *,
        name: str,
        initialize: Callable[[], None] = _noop,
        finalize: Callable[[], None] = _noop,
        idle: Callable[[], None] = _noop,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be positive, got {poll_interval_s}")

        self._name = name
        self._initialize = initialize
        self._finalize = finalize
        self._idle = idle
        self._poll_interval_s = poll_interval_s
        self._queue: queue.SimpleQueue[_WorkItem] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._pending: list[_WorkItem] = []
        self._pending_lock = threading.Lock()
        self._accepting = False
        self._failure: BaseException | None = None

    @property
    def running(self) -> bool:
        with self._lifecycle:
            return self._thread is not None and self._thread.is_alive()

    @property
    def ident(self) -> int | None:
        thread = self._thread
        return None if thread is None else thread.ident

    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive():
                    raise RuntimeError("STA thread is still running: stop() has not joined it yet")
                self._thread = None

            self._stop.clear()
            with self._pending_lock:
                self._failure = None
                self._accepting = True

            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        with self._lifecycle:
            self._stop.set()
            with self._pending_lock:
                self._accepting = False

            thread = self._thread
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    logger.warning(
                        "executor.sta_stop_timed_out", thread=thread.name, timeout=timeout
                    )
                    self._abandon(StaThreadUnavailableError(STOPPED_TEXT))
                    return False

                self._thread = None

            self._abandon(StaThreadUnavailableError(STOPPED_TEXT))
            return True

    async def call(self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def run() -> None:
            try:
                result = fn()
            except Exception as exc:
                _deliver(loop, future, exc)
                return
            _deliver(loop, future, result)

        def abandon(error: BaseException) -> None:
            _deliver(loop, future, error)

        item = _WorkItem(run, abandon)
        future.add_done_callback(lambda _: item.settled.set())

        with self._pending_lock:
            if not self._accepting:
                raise self._failure or StaThreadUnavailableError(NOT_RUNNING_TEXT)
            self._pending.append(item)
            self._queue.put(item)

        return await future

    def _run(self) -> None:
        try:
            self._initialize()
        except Exception as exc:
            logger.exception("executor.sta_initialize_failed", thread=self._name)
            self._shut(exc)
            return

        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=self._poll_interval_s)
                except queue.Empty:
                    self._run_idle()
                    continue

                if not item.settled.is_set():
                    item.run()
                self._forget(item)
        finally:
            self._shut(StaThreadUnavailableError(STOPPED_TEXT))
            try:
                self._finalize()
            except Exception:
                logger.exception("executor.sta_finalize_failed", thread=self._name)

    def _run_idle(self) -> None:
        try:
            self._idle()
        except Exception:
            logger.exception("executor.sta_idle_failed", thread=self._name)

    def _shut(self, failure: BaseException) -> None:
        with self._pending_lock:
            self._accepting = False
            if self._failure is None:
                self._failure = failure
        self._abandon(failure)

    def _abandon(self, failure: BaseException) -> None:
        with self._pending_lock:
            outstanding = tuple(self._pending)
            self._pending.clear()

        for item in outstanding:
            item.abandon(failure)

    def _forget(self, item: _WorkItem) -> None:
        with self._pending_lock:
            for index, pending in enumerate(self._pending):
                if pending is item:
                    del self._pending[index]
                    return


def _deliver(
    loop: asyncio.AbstractEventLoop, future: asyncio.Future[T], outcome: T | BaseException
) -> None:
    try:
        loop.call_soon_threadsafe(_settle, future, outcome)
    except RuntimeError:
        logger.debug("executor.sta_resolve_dropped_on_shutdown")


def _settle(future: asyncio.Future[T], outcome: T | BaseException) -> None:
    if future.done():
        return

    if isinstance(outcome, BaseException):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)


class ThreadAffinity:
    def __init__(self) -> None:
        self._owner: int | None = None

    @property
    def owner(self) -> int | None:
        return self._owner

    def bind(self) -> None:
        self._owner = threading.get_ident()

    def release(self) -> None:
        self._owner = None

    def check(self) -> None:
        owner = self._owner
        current = threading.get_ident()
        if owner is None:
            raise RuntimeError("COM object used outside its apartment: enter_thread() not called")
        if owner != current:
            raise RuntimeError(
                f"COM object owned by thread {owner} was touched from thread {current}"
            )
