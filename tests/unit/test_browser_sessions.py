from __future__ import annotations

import asyncio
import uuid

import pytest

from libs.browser import BrowserSessionManager
from libs.core.exceptions import ConfigurationError

IDLE_TTL_S = 100.0
SWEEP_INTERVAL_S = 10.0


class FakeContext:
    def __init__(self, index: int) -> None:
        self.index = index
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.starts = 0
        self.stops = 0
        self._running = False
        self._connected = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._connected

    def crash(self) -> None:
        """Браузер упал снаружи: ссылка на него у бэкенда есть, соединения нет."""
        self._connected = False

    async def new_context(self) -> FakeContext:
        if not self._running or not self._connected:
            self._running = True
            self._connected = True
            self.starts += 1
        context = FakeContext(len(self.contexts))
        self.contexts.append(context)
        return context

    async def aclose(self) -> None:
        self._connected = False
        if self._running:
            self._running = False
            self.stops += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_manager(
    backend: FakeBackend,
    clock: FakeClock,
    *,
    max_sessions: int = 4,
) -> BrowserSessionManager[FakeContext]:
    return BrowserSessionManager(
        backend,
        idle_ttl_seconds=IDLE_TTL_S,
        sweep_interval_seconds=SWEEP_INTERVAL_S,
        max_sessions=max_sessions,
        clock=clock,
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def manager(backend: FakeBackend, clock: FakeClock) -> BrowserSessionManager[FakeContext]:
    return make_manager(backend, clock)


class TestLifecycle:
    async def test_the_same_conversation_gets_the_same_context_across_calls(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        async with manager.acquire(conversation_id) as first:
            pass
        async with manager.acquire(conversation_id) as second:
            pass

        assert first is second
        assert len(backend.contexts) == 1
        assert manager.active_sessions == 1

    async def test_different_conversations_get_different_contexts(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        async with (
            manager.acquire(uuid.uuid4()) as first,
            manager.acquire(uuid.uuid4()) as second,
        ):
            assert first is not second

        assert len(backend.contexts) == 2
        assert manager.active_sessions == 2

    async def test_the_browser_starts_on_the_first_call_and_not_before(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        assert backend.starts == 0
        assert not backend.running

        async with manager.acquire(uuid.uuid4()):
            pass

        assert backend.starts == 1
        assert backend.running

    async def test_concurrent_calls_of_one_conversation_open_a_single_context(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        async def call() -> FakeContext:
            async with manager.acquire(conversation_id) as context:
                await asyncio.sleep(0)
                return context

        contexts = await asyncio.gather(*(call() for _ in range(5)))

        assert len({id(context) for context in contexts}) == 1
        assert len(backend.contexts) == 1

    async def test_closing_a_session_closes_its_context(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()
        async with manager.acquire(conversation_id):
            pass

        assert await manager.close_session(conversation_id) is True
        assert backend.contexts[0].closed
        assert manager.active_sessions == 0

    async def test_closing_an_unknown_session_is_not_an_error(
        self, manager: BrowserSessionManager[FakeContext]
    ) -> None:
        assert await manager.close_session(uuid.uuid4()) is False

    async def test_a_closed_manager_closes_everything_it_opened(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        async with manager.acquire(uuid.uuid4()):
            pass
        async with manager.acquire(uuid.uuid4()):
            pass

        await manager.aclose()

        assert all(context.closed for context in backend.contexts)
        assert manager.active_sessions == 0
        assert not backend.running

    async def test_a_closed_manager_hands_out_nothing(
        self, manager: BrowserSessionManager[FakeContext]
    ) -> None:
        await manager.aclose()

        with pytest.raises(ConfigurationError):
            async with manager.acquire(uuid.uuid4()):
                pass

    async def test_a_context_that_fails_to_close_does_not_break_the_manager(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()
        async with manager.acquire(conversation_id) as context:
            pass

        async def explode() -> None:
            raise RuntimeError("контекст уже мёртв")

        context.close = explode  # type: ignore[method-assign]  # эмуляция сбоя браузера

        assert await manager.close_session(conversation_id) is True
        assert manager.active_sessions == 0


class TestCrashRecovery:
    async def test_a_conversation_recovers_when_the_browser_died_between_calls(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        async with manager.acquire(conversation_id) as dead:
            pass

        backend.crash()

        async with manager.acquire(conversation_id) as fresh:
            pass

        assert fresh is not dead
        assert backend.starts == 2
        assert len(backend.contexts) == 2
        assert manager.active_sessions == 1

    async def test_the_context_of_a_dead_browser_is_not_handed_out_again(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        async with manager.acquire(conversation_id) as dead:
            pass

        backend.crash()

        async with manager.acquire(conversation_id):
            pass

        assert dead.closed

    async def test_a_dead_context_that_refuses_to_close_still_gets_replaced(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        async with manager.acquire(conversation_id) as dead:
            pass

        async def explode() -> None:
            raise RuntimeError("контекст умер вместе с браузером")

        dead.close = explode  # type: ignore[method-assign]  # эмуляция сбоя браузера
        backend.crash()

        async with manager.acquire(conversation_id) as fresh:
            assert fresh is not dead

        assert manager.active_sessions == 1

    async def test_a_live_browser_is_never_restarted_on_reuse(
        self, manager: BrowserSessionManager[FakeContext], backend: FakeBackend
    ) -> None:
        conversation_id = uuid.uuid4()

        for _ in range(3):
            async with manager.acquire(conversation_id):
                pass

        assert backend.starts == 1
        assert len(backend.contexts) == 1


class TestIdleSweep:
    async def test_a_context_nobody_touched_is_closed_after_the_ttl(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        async with manager.acquire(uuid.uuid4()):
            pass

        clock.advance(IDLE_TTL_S + 1)

        assert await manager.sweep_idle() == 1
        assert backend.contexts[0].closed
        assert manager.active_sessions == 0

    async def test_a_context_used_recently_survives_the_sweep(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        conversation_id = uuid.uuid4()
        async with manager.acquire(conversation_id):
            pass

        clock.advance(IDLE_TTL_S - 1)
        async with manager.acquire(conversation_id):
            pass
        clock.advance(IDLE_TTL_S - 1)

        assert await manager.sweep_idle() == 0
        assert not backend.contexts[0].closed

    async def test_a_context_still_in_use_is_never_swept_from_under_the_caller(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        async with manager.acquire(uuid.uuid4()) as context:
            clock.advance(IDLE_TTL_S * 10)

            assert await manager.sweep_idle() == 0
            assert not context.closed

        assert await manager.sweep_idle() == 0
        clock.advance(IDLE_TTL_S + 1)
        assert await manager.sweep_idle() == 1
        assert context.closed

    async def test_the_browser_stops_when_the_last_context_expires(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        async with manager.acquire(uuid.uuid4()):
            pass

        clock.advance(IDLE_TTL_S + 1)
        await manager.sweep_idle()

        assert not backend.running
        assert backend.stops == 1

    async def test_the_browser_restarts_by_itself_after_a_quiet_period(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        async with manager.acquire(uuid.uuid4()):
            pass
        clock.advance(IDLE_TTL_S + 1)
        await manager.sweep_idle()

        async with manager.acquire(uuid.uuid4()):
            pass

        assert backend.running
        assert backend.starts == 2

    async def test_the_browser_keeps_running_while_another_conversation_holds_a_context(
        self,
        manager: BrowserSessionManager[FakeContext],
        backend: FakeBackend,
        clock: FakeClock,
    ) -> None:
        stale = uuid.uuid4()
        async with manager.acquire(stale):
            pass
        clock.advance(IDLE_TTL_S - 1)
        fresh = uuid.uuid4()
        async with manager.acquire(fresh):
            pass
        clock.advance(2)

        assert await manager.sweep_idle() == 1
        assert backend.running
        assert manager.active_sessions == 1

    async def test_the_sweeper_task_runs_on_its_interval_and_stops_with_the_manager(
        self, backend: FakeBackend, clock: FakeClock
    ) -> None:
        manager = BrowserSessionManager(
            backend,
            idle_ttl_seconds=IDLE_TTL_S,
            sweep_interval_seconds=0.01,
            max_sessions=4,
            clock=clock,
        )
        async with manager.acquire(uuid.uuid4()):
            pass
        clock.advance(IDLE_TTL_S + 1)

        manager.start_sweeper()
        assert manager.sweeping
        for _ in range(100):
            await asyncio.sleep(0.01)
            if manager.active_sessions == 0:
                break

        assert manager.active_sessions == 0
        assert backend.contexts[0].closed

        await manager.aclose()
        assert not manager.sweeping

    async def test_the_sweeper_survives_a_failing_sweep(
        self, backend: FakeBackend, clock: FakeClock
    ) -> None:
        manager = BrowserSessionManager(
            backend,
            idle_ttl_seconds=IDLE_TTL_S,
            sweep_interval_seconds=0.01,
            max_sessions=4,
            clock=clock,
        )
        sweeps = 0
        original = manager.sweep_idle

        async def failing_sweep() -> int:
            nonlocal sweeps
            sweeps += 1
            if sweeps == 1:
                raise RuntimeError("Redis браузера не бывает, но сбой бывает")
            return await original()

        manager.sweep_idle = failing_sweep  # type: ignore[method-assign]  # шов вместо сбоя
        manager.start_sweeper()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if sweeps >= 2:
                break

        assert sweeps >= 2
        assert manager.sweeping

        await manager.aclose()


class TestSessionLimit:
    async def test_the_oldest_idle_context_is_evicted_at_the_limit(
        self, backend: FakeBackend, clock: FakeClock
    ) -> None:
        manager = make_manager(backend, clock, max_sessions=2)
        first, second = uuid.uuid4(), uuid.uuid4()

        async with manager.acquire(first):
            pass
        clock.advance(1)
        async with manager.acquire(second):
            pass
        clock.advance(1)
        async with manager.acquire(uuid.uuid4()):
            pass

        assert manager.active_sessions == 2
        assert backend.contexts[0].closed
        assert not backend.contexts[1].closed

    async def test_a_context_in_use_is_never_evicted_to_make_room(
        self, backend: FakeBackend, clock: FakeClock
    ) -> None:
        manager = make_manager(backend, clock, max_sessions=1)
        busy = uuid.uuid4()

        async with manager.acquire(busy) as context, manager.acquire(uuid.uuid4()):
            assert not context.closed

        assert manager.active_sessions == 2
