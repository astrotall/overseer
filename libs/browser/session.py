from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

logger = get_logger(__name__)

Clock = Callable[[], float]


class SupportsAsyncClose(Protocol):
    async def close(self) -> None: ...


ContextT = TypeVar("ContextT", bound=SupportsAsyncClose)
ContextT_co = TypeVar("ContextT_co", bound=SupportsAsyncClose, covariant=True)


class BrowserBackend(Protocol[ContextT_co]):
    @property
    def running(self) -> bool: ...

    async def new_context(self) -> ContextT_co: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class BrowserSession(Generic[ContextT]):
    conversation_id: uuid.UUID
    context: ContextT
    last_used_at: float
    leases: int = 0

    @property
    def busy(self) -> bool:
        return self.leases > 0


class BrowserSessionManager(Generic[ContextT]):
    def __init__(
        self,
        backend: BrowserBackend[ContextT],
        *,
        idle_ttl_seconds: float,
        sweep_interval_seconds: float,
        max_sessions: int,
        clock: Clock = time.monotonic,
    ) -> None:
        if idle_ttl_seconds <= 0:
            raise ValueError(f"idle_ttl_seconds must be positive, got {idle_ttl_seconds}")
        if sweep_interval_seconds <= 0:
            raise ValueError(
                f"sweep_interval_seconds must be positive, got {sweep_interval_seconds}"
            )
        if max_sessions <= 0:
            raise ValueError(f"max_sessions must be positive, got {max_sessions}")

        self._backend = backend
        self._idle_ttl_seconds = idle_ttl_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._sessions: dict[uuid.UUID, BrowserSession[ContextT]] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    @property
    def sweeping(self) -> bool:
        return self._sweeper is not None and not self._sweeper.done()

    @asynccontextmanager
    async def acquire(self, conversation_id: uuid.UUID) -> AsyncIterator[ContextT]:
        session = await self._lease(conversation_id)
        try:
            yield session.context
        finally:
            await self._release(session)

    async def close_session(self, conversation_id: uuid.UUID) -> bool:
        async with self._lock:
            session = self._sessions.pop(conversation_id, None)
            if session is None:
                return False

            await self._close_context(session, reason="closed")
            await self._stop_backend_if_unused()
            return True

    async def sweep_idle(self) -> int:
        async with self._lock:
            if self._closed:
                return 0

            deadline = self._clock() - self._idle_ttl_seconds
            expired = [
                session
                for session in self._sessions.values()
                if not session.busy and session.last_used_at <= deadline
            ]
            for session in expired:
                del self._sessions[session.conversation_id]
                await self._close_context(session, reason="idle")

            if expired:
                await self._stop_backend_if_unused()
            return len(expired)

    def start_sweeper(self) -> None:
        if self._closed:
            raise ConfigurationError("BrowserSessionManager закрыт: перезапуск не поддерживается")
        if self.sweeping:
            raise RuntimeError("Сборщик простаивающих сессий уже запущен")

        self._sweeper = asyncio.create_task(self._sweep_loop(), name="browser-session-sweeper")

    async def aclose(self) -> None:
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)

        async with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                await self._close_context(session, reason="shutdown")
            await self._backend.aclose()

    async def _lease(self, conversation_id: uuid.UUID) -> BrowserSession[ContextT]:
        async with self._lock:
            if self._closed:
                raise ConfigurationError(
                    "BrowserSessionManager закрыт: браузерная сессия недоступна"
                )

            session = self._sessions.get(conversation_id)
            if session is None:
                await self._make_room()
                context = await self._backend.new_context()
                session = BrowserSession(
                    conversation_id=conversation_id,
                    context=context,
                    last_used_at=self._clock(),
                )
                self._sessions[conversation_id] = session
                logger.info(
                    "browser.session_opened",
                    conversation_id=str(conversation_id),
                    active_sessions=len(self._sessions),
                )

            session.leases += 1
            session.last_used_at = self._clock()
            return session

    async def _release(self, session: BrowserSession[ContextT]) -> None:
        async with self._lock:
            session.leases -= 1
            session.last_used_at = self._clock()

    async def _make_room(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return

        idle = [session for session in self._sessions.values() if not session.busy]
        if not idle:
            logger.warning(
                "browser.session_limit_exceeded",
                active_sessions=len(self._sessions),
                max_sessions=self._max_sessions,
            )
            return

        victim = min(idle, key=lambda session: session.last_used_at)
        del self._sessions[victim.conversation_id]
        await self._close_context(victim, reason="evicted")

    async def _close_context(self, session: BrowserSession[ContextT], *, reason: str) -> None:
        try:
            await session.context.close()
        except Exception:
            logger.exception(
                "browser.session_close_failed",
                conversation_id=str(session.conversation_id),
                reason=reason,
            )
            return

        logger.info(
            "browser.session_closed",
            conversation_id=str(session.conversation_id),
            reason=reason,
            active_sessions=len(self._sessions),
        )

    async def _stop_backend_if_unused(self) -> None:
        if self._sessions or not self._backend.running:
            return

        try:
            await self._backend.aclose()
        except Exception:
            logger.exception("browser.backend_stop_failed")
            return

        logger.info("browser.backend_stopped")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval_seconds)
            try:
                await self.sweep_idle()
            except Exception:
                logger.exception("browser.sweep_failed")
