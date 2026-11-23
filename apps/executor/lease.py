from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger
from libs.executor.commands import eval_script
from libs.executor.keys import HEARTBEAT_KEY, OWNER_KEY
from libs.schemas.executor import ExecutorHeartbeat, encode_heartbeat

logger = get_logger(__name__)

TRANSPORT_FAILURES: Final = (RedisError, OSError)
ACQUIRE_POLL_S: Final = 1.0

ACQUIRE_SCRIPT: Final = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[3]) then
    redis.call('SET', KEYS[2], ARGV[2], 'PX', ARGV[3])
    return 1
end
return 0
"""

RENEW_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[3])
    redis.call('SET', KEYS[2], ARGV[2], 'PX', ARGV[3])
    return 1
end
return 0
"""

RELEASE_SCRIPT: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1], KEYS[2])
    return 1
end
return 0
"""

ALREADY_RUNNING_TEXT: Final = (
    "Другой executor уже подключён к этому Redis и обновляет heartbeat: второй экземпляр "
    "не запускается. Остановите первый (или подождите, если он только что упал: его "
    "владение истекает само через {ttl:g} с)"
)
OWNERSHIP_LOST_TEXT: Final = (
    "Executor потерял владение очередью (ключ владения истёк или его занял другой "
    "экземпляр): приём запросов остановлен. Перезапустите executor"
)

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]


class ExecutorAlreadyRunningError(ConfigurationError):
    pass


class OwnershipLostError(ConfigurationError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutorLease:
    def __init__(
        self,
        redis: Redis,
        *,
        operations: Iterable[str],
        ttl_s: float,
        interval_s: float,
        instance_id: uuid.UUID | None = None,
        clock: Clock = utc_now,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if ttl_s < 2 * interval_s or interval_s <= 0:
            raise ConfigurationError("TTL владения должен быть не меньше двух интервалов heartbeat")
        self._redis = redis
        self._operations = sorted(operations)
        self._ttl_ms = int(ttl_s * 1000)
        self._ttl_s = ttl_s
        self._interval_s = interval_s
        self.instance_id = instance_id or uuid.uuid4()
        self._clock = clock
        self._monotonic = monotonic
        self._started_at = clock()
        self._confirmed_until = float("-inf")
        self._lost = False
        self._held = False

    @property
    def lost(self) -> bool:
        return self._lost

    @property
    def held(self) -> bool:
        return self._held and not self._lost

    def confirmed_for(self) -> float:
        if not self.held:
            return 0.0
        return max(0.0, self._confirmed_until - self._monotonic())

    async def acquire(self, *, wait_s: float | None = None) -> None:
        give_up = self._monotonic() + (self._ttl_s + self._interval_s if wait_s is None else wait_s)
        observed: str | None = None
        while True:
            started = self._monotonic()
            if await self._script(ACQUIRE_SCRIPT):
                self._held = True
                self._lost = False
                self._confirm(started)
                logger.info("executor.ownership_acquired", instance_id=str(self.instance_id))
                return

            current = await self._redis.get(HEARTBEAT_KEY)
            if observed is not None and current is not None and current != observed:
                raise ExecutorAlreadyRunningError(ALREADY_RUNNING_TEXT.format(ttl=self._ttl_s))
            if current is not None:
                observed = str(current)
            if self._monotonic() >= give_up:
                raise ExecutorAlreadyRunningError(ALREADY_RUNNING_TEXT.format(ttl=self._ttl_s))
            logger.info("executor.ownership_waiting", ttl_s=self._ttl_s)
            await asyncio.sleep(min(ACQUIRE_POLL_S, self._interval_s))

    async def renew(self) -> bool:
        if not self.held:
            return False
        started = self._monotonic()
        if await self._script(RENEW_SCRIPT):
            self._confirm(started)
            return True
        self._lost = True
        logger.error("executor.ownership_lost", instance_id=str(self.instance_id))
        return False

    async def keep_alive(self) -> None:
        while self.held:
            await asyncio.sleep(self._interval_s)
            try:
                await self.renew()
            except TRANSPORT_FAILURES as exc:
                logger.warning(
                    "executor.heartbeat_failed",
                    error=type(exc).__name__,
                    confirmed_for_s=round(self.confirmed_for(), 1),
                )

    async def release(self) -> bool:
        if not self._held:
            return False
        self._held = False
        try:
            released = bool(
                await eval_script(
                    self._redis, RELEASE_SCRIPT, [OWNER_KEY, HEARTBEAT_KEY], [str(self.instance_id)]
                )
            )
        except TRANSPORT_FAILURES as exc:
            logger.warning("executor.ownership_release_failed", error=type(exc).__name__)
            return False
        logger.info("executor.ownership_released", released=released)
        return released

    def _confirm(self, started: float) -> None:
        self._confirmed_until = started + self._ttl_s - self._interval_s

    async def _script(self, script: str) -> bool:
        heartbeat = encode_heartbeat(
            ExecutorHeartbeat(
                instance_id=self.instance_id,
                started_at=self._started_at,
                sent_at=self._clock(),
                operations=self._operations,
            )
        )
        result = await eval_script(
            self._redis,
            script,
            [OWNER_KEY, HEARTBEAT_KEY],
            [str(self.instance_id), heartbeat, str(self._ttl_ms)],
        )
        return bool(result)
