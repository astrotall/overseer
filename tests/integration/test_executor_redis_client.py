from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

import pytest
import structlog
from redis.asyncio import Redis
from structlog.testing import capture_logs

from libs.executor.commands import blpop, rpush
from libs.executor.keys import HEARTBEAT_KEY, REQUESTS_KEY, reply_key
from libs.executor.redis_client import RedisExecutorClient
from libs.schemas.executor import (
    ExecutorAccepted,
    ExecutorRequest,
    ExecutorResponse,
    encode_reply,
    parse_request,
)
from tests.executor_support import heartbeat, list_length, purge_executor_keys, write_request

pytestmark = pytest.mark.integration

PICKUP_S = 0.4
SLICE_S = 0.2
FAST_FAILURE_BOUND_S = 0.25

Script = Callable[[Redis, ExecutorRequest], Awaitable[None]]


@pytest.fixture
async def redis(redis_client: Redis) -> AsyncIterator[Redis]:
    await purge_executor_keys(redis_client)
    yield redis_client
    await purge_executor_keys(redis_client)


def make_client(redis: Redis, **overrides: Any) -> RedisExecutorClient:
    options: dict[str, Any] = {
        "pickup_timeout_s": PICKUP_S,
        "poll_slice_s": SLICE_S,
        "call_budget_s": 10.0,
    }
    options.update(overrides)
    return RedisExecutorClient(redis, **options)


async def publish_heartbeat(redis: Redis, instance_id: uuid.UUID, ttl_s: float = 30.0) -> None:
    await redis.set(HEARTBEAT_KEY, heartbeat(instance_id), px=int(ttl_s * 1000))


async def accept_and_answer(redis: Redis, request: ExecutorRequest) -> None:
    key = reply_key(request.id)
    await rpush(redis, key, encode_reply(ExecutorAccepted(id=request.id)))
    await rpush(redis, key, encode_reply(ExecutorResponse.ok(request.id, {"done": True})))


async def serve_one(redis: Redis, script: Script, *, block_s: float = 5.0) -> ExecutorRequest:
    raw = await blpop(redis, REQUESTS_KEY, block_s)
    assert raw is not None, "запрос так и не попал в очередь"
    request = parse_request(raw)
    await script(redis, request)
    return request


async def queued(redis: Redis, length: int) -> None:
    for _ in range(500):
        if await list_length(redis, REQUESTS_KEY) >= length:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"в очереди так и не набралось {length} запросов")


async def test_a_missing_heartbeat_fails_at_once_without_queueing(redis: Redis) -> None:
    client = make_client(redis)
    request = write_request()

    started = time.monotonic()
    response = await client.call(request)
    elapsed = time.monotonic() - started

    assert response.error is not None
    assert response.error.code == "executor_unavailable"
    assert response.error.outcome == "not_executed"
    assert elapsed < FAST_FAILURE_BOUND_S
    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_a_live_executor_answers_through_the_reply_list(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    client = make_client(redis)
    request = write_request()

    server = asyncio.create_task(serve_one(redis, accept_and_answer))
    response = await client.call(request)

    assert (await server).id == request.id
    assert response.status == "ok"
    assert response.data == {"done": True}
    assert await redis.exists(reply_key(request.id)) == 0


async def test_a_request_nobody_takes_is_withdrawn_and_reported_not_executed(
    redis: Redis,
) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    client = make_client(redis)

    started = time.monotonic()
    response = await client.call(write_request())
    elapsed = time.monotonic() - started

    assert response.error is not None
    assert response.error.code == "not_picked_up"
    assert response.error.outcome == "not_executed"
    assert PICKUP_S <= elapsed < PICKUP_S + 0.5
    assert await list_length(redis, REQUESTS_KEY) == 0


def barrier_redis_class(reached: asyncio.Event, release: asyncio.Event) -> type[Redis]:
    class BarrierRedis(Redis):
        lrem_reached: ClassVar[asyncio.Event] = reached
        lrem_release: ClassVar[asyncio.Event] = release

        async def execute_command(self, *args: Any, **options: Any) -> Any:
            if args and args[0] == "LREM":
                self.lrem_reached.set()
                await self.lrem_release.wait()
            return await super().execute_command(*args, **options)

    return BarrierRedis


async def test_the_withdrawal_wins_the_race_and_the_executor_finds_nothing(
    redis: Redis,
) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    reached, release = asyncio.Event(), asyncio.Event()
    barrier = barrier_redis_class(reached, release)(connection_pool=redis.connection_pool)
    client = make_client(barrier)

    call = asyncio.create_task(client.call(write_request()))
    await asyncio.wait_for(reached.wait(), 5)
    release.set()
    response = await call

    assert response.error is not None
    assert response.error.code == "not_picked_up"
    assert await blpop(redis, REQUESTS_KEY, 0.3) is None


async def test_the_pickup_wins_the_race_and_the_client_waits_for_the_result(
    redis: Redis,
) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    reached, release = asyncio.Event(), asyncio.Event()
    barrier = barrier_redis_class(reached, release)(connection_pool=redis.connection_pool)
    client = make_client(barrier)
    taken = asyncio.Event()

    async def take_then_answer(redis: Redis, request: ExecutorRequest) -> None:
        taken.set()
        await release.wait()
        await accept_and_answer(redis, request)

    call = asyncio.create_task(client.call(write_request()))
    await asyncio.wait_for(reached.wait(), 5)
    server = asyncio.create_task(serve_one(redis, take_then_answer))
    await asyncio.wait_for(taken.wait(), 5)
    release.set()
    response = await call
    await server

    assert response.status == "ok"
    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_an_executor_that_dies_mid_call_is_noticed_by_the_heartbeat(
    redis: Redis,
) -> None:
    ttl_s = 0.8
    instance = uuid.uuid4()
    await publish_heartbeat(redis, instance, ttl_s)
    client = make_client(redis, call_budget_s=30.0)

    async def accept_then_die(redis: Redis, request: ExecutorRequest) -> None:
        await rpush(redis, reply_key(request.id), encode_reply(ExecutorAccepted(id=request.id)))

    call = asyncio.create_task(client.call(write_request(budget_s=30.0)))
    await serve_one(redis, accept_then_die)
    died = time.monotonic()
    response = await call
    noticed_after = time.monotonic() - died

    assert response.error is not None
    assert response.error.code == "reply_timeout"
    assert response.error.outcome == "unknown"
    assert noticed_after < ttl_s + SLICE_S + 0.5


async def test_a_restarted_executor_is_noticed_by_its_new_instance_id(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    client = make_client(redis, call_budget_s=30.0)

    async def accept_then_restart(redis: Redis, request: ExecutorRequest) -> None:
        await rpush(redis, reply_key(request.id), encode_reply(ExecutorAccepted(id=request.id)))
        await publish_heartbeat(redis, uuid.uuid4())

    call = asyncio.create_task(client.call(write_request(budget_s=30.0)))
    await serve_one(redis, accept_then_restart)
    started = time.monotonic()
    response = await call

    assert response.error is not None
    assert response.error.code == "reply_timeout"
    assert time.monotonic() - started < SLICE_S + 0.5


async def test_a_live_but_silent_executor_costs_the_budget_and_no_more(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    budget_s = 1.0
    client = make_client(redis, call_budget_s=budget_s)

    async def accept_only(redis: Redis, request: ExecutorRequest) -> None:
        await rpush(redis, reply_key(request.id), encode_reply(ExecutorAccepted(id=request.id)))

    started = time.monotonic()
    call = asyncio.create_task(client.call(write_request()))
    await serve_one(redis, accept_only)
    response = await call

    assert response.error is not None
    assert response.error.code == "reply_timeout"
    assert budget_s <= time.monotonic() - started < budget_s + 0.5


async def test_a_heartbeat_of_another_schema_version_is_refused_before_sending(
    redis: Redis,
) -> None:
    document = json.loads(heartbeat(uuid.uuid4()))
    document["schema_version"] = 2
    await redis.set(HEARTBEAT_KEY, json.dumps(document), ex=30)

    response = await make_client(redis).call(write_request())

    assert response.error is not None
    assert response.error.code == "unsupported_version"
    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_an_operation_the_executor_does_not_advertise_is_refused_before_sending(
    redis: Redis,
) -> None:
    await redis.set(
        HEARTBEAT_KEY,
        heartbeat(uuid.uuid4(), operations=["excel.save", "excel.open_or_create_workbook"]),
        ex=30,
    )

    response = await make_client(redis).call(write_request())

    assert response.error is not None
    assert response.error.code == "unknown_operation"
    assert await list_length(redis, REQUESTS_KEY) == 0


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda reply: {**reply, "message": "C:\\Users\\ivan\\secret.xlsx"}, id="top"),
        pytest.param(
            lambda reply: {
                **reply,
                "status": "error",
                "data": {},
                "error": {"code": "com_error", "message": "COM said something"},
            },
            id="inside-error",
        ),
        pytest.param(lambda reply: {**reply, "id": str(uuid.uuid4())}, id="foreign-id"),
        pytest.param(lambda reply: {**reply, "type": "result", "status": "maybe"}, id="shape"),
    ],
)
async def test_a_reply_outside_the_contract_is_rejected(
    redis: Redis, tamper: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    await publish_heartbeat(redis, uuid.uuid4())

    async def answer_badly(redis: Redis, request: ExecutorRequest) -> None:
        reply = json.loads(encode_reply(ExecutorResponse.ok(request.id)))
        await rpush(redis, reply_key(request.id), json.dumps(tamper(reply)))

    call = asyncio.create_task(make_client(redis).call(write_request()))
    await serve_one(redis, answer_badly)
    response = await call

    assert response.error is not None
    assert response.error.code == "malformed_reply"
    assert response.error.outcome == "unknown"


async def test_an_oversized_request_never_reaches_redis(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    request = write_request(data=[["я" * 32_767] for _ in range(40)])

    response = await make_client(redis).call(request)

    assert response.error is not None
    assert response.error.code == "payload_too_large"
    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_an_unreachable_redis_is_a_fast_not_executed_failure() -> None:
    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=1)
    started = time.monotonic()
    response = await make_client(unreachable).call(write_request())
    await unreachable.aclose()

    assert response.error is not None
    assert response.error.code == "transport_unavailable"
    assert response.error.outcome == "not_executed"
    assert time.monotonic() - started < 1.0


async def test_a_cancelled_call_takes_its_request_back(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    call = asyncio.create_task(make_client(redis, pickup_timeout_s=5.0).call(write_request()))
    await queued(redis, 1)

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_calls_beyond_the_concurrency_cap_are_turned_away_not_queued(
    redis: Redis,
) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    client = make_client(redis, max_concurrent_calls=1, call_budget_s=5.0)

    async def accept_only(redis: Redis, request: ExecutorRequest) -> None:
        await rpush(redis, reply_key(request.id), encode_reply(ExecutorAccepted(id=request.id)))

    first = asyncio.create_task(client.call(write_request()))
    await serve_one(redis, accept_only)
    with capture_logs() as events:
        second = await client.call(write_request())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert second.error is not None
    assert second.error.code == "not_picked_up"
    assert any(event.get("reason") == "client_busy" for event in events)
    assert await list_length(redis, REQUESTS_KEY) == 0


async def test_two_queued_requests_are_served_in_order(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    client = make_client(redis, pickup_timeout_s=3.0)
    first, second = write_request(), write_request()

    calls = [asyncio.create_task(client.call(first))]
    await queued(redis, 1)
    calls.append(asyncio.create_task(client.call(second)))
    await queued(redis, 2)

    served = [await serve_one(redis, accept_and_answer) for _ in range(2)]
    responses = await asyncio.gather(*calls)

    assert [request.id for request in served] == [first.id, second.id]
    assert all(response.status == "ok" for response in responses)


async def test_payload_contents_never_reach_the_logs(redis: Redis) -> None:
    await publish_heartbeat(redis, uuid.uuid4())
    secret = "Зарплата-Иванова-123456"
    request = write_request(sheet="Тайный лист", data=[[secret]])

    with capture_logs([structlog.processors.format_exc_info]) as events:
        call = asyncio.create_task(make_client(redis).call(request))
        await serve_one(redis, accept_and_answer)
        await call
        await make_client(redis).call(write_request(data=[[secret]]))

    rendered = json.dumps(events, ensure_ascii=False, default=str)
    assert secret not in rendered
    assert "Тайный лист" not in rendered
    finished = [event for event in events if event["event"] == "executor.call_finished"]
    assert finished[0]["request_id"] == str(request.id)
    assert finished[0]["operation"] == "excel.write_range"
