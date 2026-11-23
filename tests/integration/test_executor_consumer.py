from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from pydantic import JsonValue
from redis.asyncio import Redis
from structlog.testing import capture_logs

from apps.executor.consumer import ExecutorConsumer, ExecutorService, RequestRefusedError
from apps.executor.lease import ExecutorAlreadyRunningError, ExecutorLease, OwnershipLostError
from libs.core.exceptions import OfficeFileLockedError
from libs.executor.commands import blpop, rpush
from libs.executor.keys import HEARTBEAT_KEY, OWNER_KEY, REQUESTS_KEY, reply_key
from libs.executor.redis_client import RedisExecutorClient
from libs.schemas.executor import (
    OPERATIONS,
    ExecutorRequest,
    WriteRangeArguments,
    WriteRangeRequest,
    encode_request,
    parse_heartbeat,
    parse_reply,
)
from tests.executor_support import heartbeat, list_length, purge_executor_keys, write_request

pytestmark = pytest.mark.integration

TTL_S = 1.0
INTERVAL_S = 0.25
POLL_S = 0.1
REPLY_TTL_S = 30

Behaviour = Callable[[ExecutorRequest], Awaitable[dict[str, JsonValue]]]


async def succeed(request: ExecutorRequest) -> dict[str, JsonValue]:
    return {"served": str(request.id)}


class FakeDispatcher:
    def __init__(self, behaviour: Behaviour = succeed) -> None:
        self.behaviour = behaviour
        self.calls: list[ExecutorRequest] = []
        self.started = False
        self.closed = False
        self.idles = 0

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(OPERATIONS)

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def idle(self) -> None:
        self.idles += 1

    async def dispatch(self, request: ExecutorRequest) -> dict[str, JsonValue]:
        self.calls.append(request)
        return await self.behaviour(request)


def make_lease(redis: Redis, **overrides: Any) -> ExecutorLease:
    options: dict[str, Any] = {"ttl_s": TTL_S, "interval_s": INTERVAL_S}
    options.update(overrides)
    return ExecutorLease(redis, operations=OPERATIONS, **options)


def make_service(
    redis: Redis,
    dispatcher: FakeDispatcher,
    *,
    max_call_s: float = 5.0,
    acquire_wait_s: float | None = None,
    lease: ExecutorLease | None = None,
) -> tuple[ExecutorService, ExecutorConsumer, ExecutorLease]:
    lease = lease or make_lease(redis)
    consumer = ExecutorConsumer(
        redis,
        dispatcher,
        lease,
        reply_ttl_s=REPLY_TTL_S,
        dedup_ttl_s=600.0,
        max_call_s=max_call_s,
        poll_timeout_s=POLL_S,
    )
    return (
        ExecutorService(consumer, lease, dispatcher, acquire_wait_s=acquire_wait_s),
        consumer,
        lease,
    )


@contextlib.asynccontextmanager
async def running(
    service: ExecutorService, lease: ExecutorLease
) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(service.run())
    for _ in range(300):
        if lease.held or task.done():
            break
        await asyncio.sleep(0.01)
    try:
        yield task
    finally:
        service.request_stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)


async def held(lease: ExecutorLease) -> None:
    for _ in range(300):
        if lease.held:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("executor так и не получил владение")


@pytest.fixture
async def redis(redis_client: Redis) -> AsyncIterator[Redis]:
    await purge_executor_keys(redis_client)
    yield redis_client
    await purge_executor_keys(redis_client)


def client(redis: Redis, **overrides: Any) -> RedisExecutorClient:
    options: dict[str, Any] = {"pickup_timeout_s": 1.0, "poll_slice_s": 0.2, "call_budget_s": 10.0}
    options.update(overrides)
    return RedisExecutorClient(redis, **options)


async def replies(redis: Redis, request_id: uuid.UUID, count: int) -> list[Any]:
    collected: list[Any] = []
    for _ in range(count):
        raw = await blpop(redis, reply_key(request_id), 3.0)
        assert raw is not None, f"ответ #{len(collected) + 1} на {request_id} не пришёл"
        collected.append(parse_reply(raw))
    return collected


async def test_a_request_goes_from_the_client_through_the_consumer_and_back(
    redis: Redis,
) -> None:
    dispatcher = FakeDispatcher()
    service, _, lease = make_service(redis, dispatcher)
    request = write_request()

    async with running(service, lease):
        response = await client(redis).call(request)

    assert response.status == "ok"
    assert response.data == {"served": str(request.id)}
    assert [call.id for call in dispatcher.calls] == [request.id]
    assert dispatcher.started
    assert dispatcher.closed


async def test_replies_are_accepted_then_result_and_expire_on_their_own(redis: Redis) -> None:
    service, _, lease = make_service(redis, FakeDispatcher())
    request = write_request()

    async with running(service, lease):
        await rpush(redis, REQUESTS_KEY, encode_request(request))
        for _ in range(300):
            if await list_length(redis, reply_key(request.id)) == 2:
                break
            await asyncio.sleep(0.01)
        ttl = await redis.ttl(reply_key(request.id))
        accepted, result = await replies(redis, request.id, 2)

    assert 0 < ttl <= REPLY_TTL_S
    assert accepted.type == "accepted"
    assert result.type == "result"
    assert result.status == "ok"


async def test_a_duplicate_request_id_is_executed_once_and_answered_once(redis: Redis) -> None:
    dispatcher = FakeDispatcher()
    service, consumer, lease = make_service(redis, dispatcher)
    request = write_request()
    payload = encode_request(request)

    async with running(service, lease):
        await rpush(redis, REQUESTS_KEY, payload)
        await rpush(redis, REQUESTS_KEY, payload)
        for _ in range(300):
            if consumer.served >= 2:
                break
            await asyncio.sleep(0.01)

    assert len(dispatcher.calls) == 1
    assert await list_length(redis, reply_key(request.id)) == 2


async def test_an_expired_request_is_refused_without_execution(redis: Redis) -> None:
    dispatcher = FakeDispatcher()
    service, _, lease = make_service(redis, dispatcher)
    issued_at = datetime.now(UTC) - timedelta(seconds=30)
    request = WriteRangeRequest(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        issued_at=issued_at,
        deadline=issued_at + timedelta(seconds=10),
        arguments=WriteRangeArguments(sheet="Лист1", start_cell="A1", data=[["x"]]),
    )

    async with running(service, lease):
        await rpush(redis, REQUESTS_KEY, encode_request(request))
        (result,) = await replies(redis, request.id, 1)

    assert result.error.code == "deadline_exceeded"
    assert dispatcher.calls == []


def raw_request(**overrides: Any) -> tuple[uuid.UUID, str]:
    document = json.loads(encode_request(write_request()))
    document.update(overrides)
    return uuid.UUID(document["id"]), json.dumps(document)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"schema_version": 2}, "unsupported_version", id="version"),
        pytest.param({"operation": "shell.exec"}, "unknown_operation", id="operation"),
        pytest.param({"arguments": {"sheet": "S"}}, "malformed_request", id="shape"),
        pytest.param({"extra": "field"}, "malformed_request", id="extra-field"),
    ],
)
async def test_a_request_outside_the_contract_is_answered_with_its_code(
    redis: Redis, overrides: dict[str, Any], code: str
) -> None:
    dispatcher = FakeDispatcher()
    service, _, lease = make_service(redis, dispatcher)
    request_id, payload = raw_request(**overrides)

    async with running(service, lease):
        await rpush(redis, REQUESTS_KEY, payload)
        (result,) = await replies(redis, request_id, 1)

    assert result.error.code == code
    assert dispatcher.calls == []


@pytest.mark.parametrize(
    "garbage",
    [
        pytest.param("{not json", id="malformed-json"),
        pytest.param("[1, 2, 3]", id="not-an-object"),
        pytest.param("x" * (1_048_576 + 1), id="oversized"),
    ],
)
async def test_garbage_in_the_queue_does_not_stop_the_loop(redis: Redis, garbage: str) -> None:
    dispatcher = FakeDispatcher()
    service, _, lease = make_service(redis, dispatcher)
    request = write_request()

    async with running(service, lease):
        await rpush(redis, REQUESTS_KEY, garbage)
        response = await client(redis).call(request)

    assert response.status == "ok"
    assert [call.id for call in dispatcher.calls] == [request.id]


async def test_a_bug_in_a_handler_is_logged_in_full_and_answered_with_a_bare_code(
    redis: Redis,
) -> None:
    async def explode(request: ExecutorRequest) -> dict[str, JsonValue]:
        if len(dispatcher.calls) == 1:
            raise RuntimeError("C:\\Users\\ivan\\Зарплаты.xlsx exploded")
        return await succeed(request)

    dispatcher = FakeDispatcher(explode)
    service, _, lease = make_service(redis, dispatcher)

    with capture_logs([structlog.processors.format_exc_info]) as events:
        async with running(service, lease):
            failed = await client(redis).call(write_request())
            recovered = await client(redis).call(write_request())

    assert failed.error is not None
    assert failed.error.code == "internal_error"
    assert "Зарплаты" not in failed.model_dump_json()
    assert recovered.status == "ok"
    crash = next(event for event in events if event["event"] == "executor.call_failed")
    assert "Зарплаты" in crash["exception"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        pytest.param(OfficeFileLockedError("файл занят"), "file_locked", id="office"),
        pytest.param(RequestRefusedError("path_not_allowed"), "path_not_allowed", id="refused"),
    ],
)
async def test_typed_failures_become_their_contract_codes(
    redis: Redis, error: Exception, code: str
) -> None:
    async def fail(request: ExecutorRequest) -> dict[str, JsonValue]:
        raise error

    service, _, lease = make_service(redis, FakeDispatcher(fail))

    async with running(service, lease):
        response = await client(redis).call(write_request())

    assert response.error is not None
    assert response.error.code == code


async def test_a_call_over_the_executor_cap_is_reported_as_a_timeout(redis: Redis) -> None:
    async def hang(request: ExecutorRequest) -> dict[str, JsonValue]:
        await asyncio.sleep(30)
        return {}

    service, _, lease = make_service(redis, FakeDispatcher(hang), max_call_s=0.3)

    async with running(service, lease):
        response = await client(redis).call(write_request())

    assert response.error is not None
    assert response.error.code == "timeout"
    assert response.error.outcome == "unknown"


async def test_two_queued_requests_are_executed_one_after_the_other_in_order(
    redis: Redis,
) -> None:
    active = 0
    overlap = False

    async def slow(request: ExecutorRequest) -> dict[str, JsonValue]:
        nonlocal active, overlap
        active += 1
        overlap = overlap or active > 1
        await asyncio.sleep(0.1)
        active -= 1
        return {}

    dispatcher = FakeDispatcher(slow)
    service, _, lease = make_service(redis, dispatcher)
    first, second = write_request(), write_request()

    await rpush(redis, REQUESTS_KEY, encode_request(first))
    await rpush(redis, REQUESTS_KEY, encode_request(second))
    async with running(service, lease):
        await replies(redis, first.id, 2)
        await replies(redis, second.id, 2)

    assert [call.id for call in dispatcher.calls] == [first.id, second.id]
    assert not overlap


async def test_the_heartbeat_keeps_beating_during_a_long_call(redis: Redis) -> None:
    beats: set[str] = set()

    async def long_call(request: ExecutorRequest) -> dict[str, JsonValue]:
        for _ in range(8):
            beats.add(str(await redis.get(HEARTBEAT_KEY)))
            await asyncio.sleep(INTERVAL_S / 2)
        return {}

    service, _, lease = make_service(redis, FakeDispatcher(long_call))

    async with running(service, lease):
        response = await client(redis).call(write_request())

    assert response.status == "ok"
    assert len(beats) >= 3


async def test_a_second_executor_is_refused_while_the_first_is_alive(redis: Redis) -> None:
    first, _, first_lease = make_service(redis, FakeDispatcher())
    second_dispatcher = FakeDispatcher()
    second, _, _ = make_service(redis, second_dispatcher, acquire_wait_s=10.0)

    async with running(first, first_lease):
        started = time.monotonic()
        with pytest.raises(ExecutorAlreadyRunningError):
            await second.run()
        refused_after = time.monotonic() - started
        owner = await redis.get(OWNER_KEY)

    assert refused_after < 2 * INTERVAL_S + 1.0
    assert owner == str(first_lease.instance_id)
    assert not second_dispatcher.started


async def test_a_restart_right_after_a_crash_waits_for_the_stale_lock_to_expire(
    redis: Redis,
) -> None:
    dead = uuid.uuid4()
    await redis.set(OWNER_KEY, str(dead), px=int(TTL_S * 1000))
    await redis.set(HEARTBEAT_KEY, heartbeat(dead), px=int(TTL_S * 1000))
    service, _, lease = make_service(redis, FakeDispatcher())

    started = time.monotonic()
    async with running(service, lease) as task:
        for _ in range(300):
            if lease.held or task.done():
                break
            await asyncio.sleep(0.01)
        acquired_after = time.monotonic() - started
        owner = await redis.get(OWNER_KEY)

    assert lease.instance_id != dead
    assert owner == str(lease.instance_id)
    assert acquired_after < TTL_S + 1.5


async def test_an_executor_that_loses_ownership_stops_consuming(redis: Redis) -> None:
    dispatcher = FakeDispatcher()
    service, _, lease = make_service(redis, dispatcher)
    usurper = str(uuid.uuid4())

    task = asyncio.create_task(service.run())
    await held(lease)
    await redis.set(OWNER_KEY, usurper, ex=30)

    with pytest.raises(OwnershipLostError):
        await asyncio.wait_for(task, 5)
    await rpush(redis, REQUESTS_KEY, encode_request(write_request()))
    await asyncio.sleep(3 * POLL_S)

    assert await list_length(redis, REQUESTS_KEY) == 1
    assert await redis.get(OWNER_KEY) == usurper
    assert dispatcher.calls == []
    assert dispatcher.closed


async def test_a_graceful_stop_finishes_the_call_in_flight_then_releases_the_lock(
    redis: Redis,
) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def blocking(request: ExecutorRequest) -> dict[str, JsonValue]:
        entered.set()
        await finish.wait()
        return {"finished": True}

    dispatcher = FakeDispatcher(blocking)
    service, _, lease = make_service(redis, dispatcher)
    task = asyncio.create_task(service.run())
    await held(lease)
    call = asyncio.create_task(client(redis).call(write_request()))
    await asyncio.wait_for(entered.wait(), 5)

    service.request_stop()
    await asyncio.sleep(3 * POLL_S)
    assert not task.done()
    assert await redis.get(OWNER_KEY) == str(lease.instance_id)
    finish.set()
    await asyncio.wait_for(task, 5)
    response = await call

    assert response.status == "ok"
    assert response.data == {"finished": True}
    assert await redis.exists(OWNER_KEY, HEARTBEAT_KEY) == 0
    assert dispatcher.closed


async def test_cancelling_the_service_still_lets_the_call_in_flight_finish(redis: Redis) -> None:
    entered = asyncio.Event()

    async def short(request: ExecutorRequest) -> dict[str, JsonValue]:
        entered.set()
        await asyncio.sleep(0.3)
        return {"finished": True}

    service, _, lease = make_service(redis, FakeDispatcher(short))
    task = asyncio.create_task(service.run())
    await held(lease)
    call = asyncio.create_task(client(redis).call(write_request()))
    await asyncio.wait_for(entered.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    response = await call

    assert response.status == "ok"
    assert await redis.exists(OWNER_KEY, HEARTBEAT_KEY) == 0


async def test_an_executor_killed_mid_call_costs_the_heartbeat_ttl_not_the_budget(
    redis: Redis,
) -> None:
    entered = asyncio.Event()

    async def forever(request: ExecutorRequest) -> dict[str, JsonValue]:
        entered.set()
        await asyncio.sleep(60)
        return {}

    dispatcher = FakeDispatcher(forever)
    lease = make_lease(redis)
    consumer = ExecutorConsumer(
        redis, dispatcher, lease, reply_ttl_s=5, dedup_ttl_s=600.0, max_call_s=60.0
    )
    await lease.acquire()
    keeper = asyncio.create_task(lease.keep_alive())
    loop = asyncio.create_task(consumer.run())
    call = asyncio.create_task(client(redis, call_budget_s=60.0).call(write_request(budget_s=60)))
    await asyncio.wait_for(entered.wait(), 5)

    keeper.cancel()
    killed = time.monotonic()
    response = await call
    noticed_after = time.monotonic() - killed
    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop

    assert response.error is not None
    assert response.error.code == "reply_timeout"
    assert noticed_after < TTL_S + 0.2 + 0.5


async def test_the_heartbeat_advertises_the_dispatcher_operations(redis: Redis) -> None:
    service, _, lease = make_service(redis, FakeDispatcher())

    async with running(service, lease):
        raw = await redis.get(HEARTBEAT_KEY)

    assert raw is not None
    parsed = parse_heartbeat(raw)
    assert parsed.instance_id == lease.instance_id
    assert set(parsed.operations) == set(OPERATIONS)
