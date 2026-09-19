from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import cast

import pytest
from structlog.testing import capture_logs

from libs.browser import BrowserSessionManager, browser_page
from libs.browser import backend as backend_module

CLOSE_TIMEOUT_S = 0.05
CALL_DEADLINE_S = 5.0


class FakePage:
    def __init__(
        self, *, hang_on_close: bool = False, close_error: Exception | None = None
    ) -> None:
        self.closed = False
        self._hang_on_close = hang_on_close
        self._close_error = close_error

    async def close(self) -> None:
        if self._hang_on_close:
            await asyncio.Event().wait()
        if self._close_error is not None:
            raise self._close_error
        self.closed = True


PageFactory = Callable[[], FakePage]


class FakeContext:
    def __init__(self, page_factory: PageFactory) -> None:
        self.closed = False
        self._page_factory = page_factory

    async def new_page(self) -> FakePage:
        return self._page_factory()

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self, page_factory: PageFactory) -> None:
        self.contexts: list[FakeContext] = []
        self._page_factory = page_factory
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._running

    async def new_context(self) -> FakeContext:
        self._running = True
        context = FakeContext(self._page_factory)
        self.contexts.append(context)
        return context

    async def aclose(self) -> None:
        self._running = False


def install_manager(
    monkeypatch: pytest.MonkeyPatch, page_factory: PageFactory
) -> tuple[BrowserSessionManager[FakeContext], FakeBackend]:
    backend = FakeBackend(page_factory)
    manager = BrowserSessionManager(
        backend, idle_ttl_seconds=100.0, sweep_interval_seconds=10.0, max_sessions=4
    )
    monkeypatch.setattr(backend_module, "_manager", manager)
    monkeypatch.setattr(backend_module, "PAGE_CLOSE_TIMEOUT_S", CLOSE_TIMEOUT_S)
    return manager, backend


def warnings(logs: list[dict[str, object]]) -> list[object]:
    return [entry["event"] for entry in logs if entry["log_level"] == "warning"]


async def test_the_page_is_closed_when_the_call_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = install_manager(monkeypatch, FakePage)

    async with browser_page(uuid.uuid4()) as page:
        assert not cast(FakePage, page).closed

    assert cast(FakePage, page).closed
    assert manager.active_sessions == 1


async def test_a_page_that_never_closes_does_not_hang_the_call_and_takes_the_session_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, backend = install_manager(monkeypatch, lambda: FakePage(hang_on_close=True))
    conversation_id = uuid.uuid4()

    with capture_logs() as logs:
        async with asyncio.timeout(CALL_DEADLINE_S), browser_page(conversation_id):
            pass

    (stuck,) = backend.contexts
    assert stuck.closed
    assert manager.active_sessions == 0
    assert warnings(logs) == ["browser.page_close_timed_out"]


async def test_the_conversation_gets_a_fresh_context_after_its_session_was_torn_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, backend = install_manager(monkeypatch, lambda: FakePage(hang_on_close=True))
    conversation_id = uuid.uuid4()

    async with asyncio.timeout(CALL_DEADLINE_S):
        async with browser_page(conversation_id):
            pass
        async with browser_page(conversation_id):
            pass

    first, second = backend.contexts
    assert first.closed
    assert second is not first


async def test_the_error_of_the_call_survives_a_page_that_never_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_manager(monkeypatch, lambda: FakePage(hang_on_close=True))

    with pytest.raises(ValueError, match="boom"):
        async with asyncio.timeout(CALL_DEADLINE_S), browser_page(uuid.uuid4()):
            raise ValueError("boom")


async def test_a_page_that_fails_to_close_is_only_logged_and_keeps_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, backend = install_manager(
        monkeypatch, lambda: FakePage(close_error=RuntimeError("Target closed"))
    )

    with capture_logs() as logs:
        async with browser_page(uuid.uuid4()):
            pass

    assert manager.active_sessions == 1
    assert not backend.contexts[0].closed
    assert warnings(logs) == ["browser.page_close_failed"]
