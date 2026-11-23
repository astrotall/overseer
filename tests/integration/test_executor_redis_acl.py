from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import JsonValue
from redis.asyncio import Redis, from_url
from redis.exceptions import NoPermissionError, ResponseError

from apps.executor.consumer import ExecutorConsumer, ExecutorService
from apps.executor.lease import ExecutorLease
from libs.executor.commands import blpop, rpush
from libs.executor.keys import REQUESTS_KEY, reply_key, seen_key
from libs.executor.redis_client import RedisExecutorClient
from libs.schemas.executor import OPERATIONS, ExecutorRequest
from tests.executor_support import purge_executor_keys, write_request

pytestmark = pytest.mark.integration

RULES = Path(__file__).resolve().parents[2] / "docker" / "redis" / "executor.acl"
PASSWORD = "acl-test-password-not-a-secret-0123456789"

Probe = Callable[[Redis], Awaitable[Any]]


class EchoDispatcher:
    def __init__(self) -> None:
        self.calls: list[ExecutorRequest] = []

    @property
    def operations(self) -> frozenset[str]:
        return frozenset(OPERATIONS)

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def idle(self) -> None:
        return None

    async def dispatch(self, request: ExecutorRequest) -> dict[str, JsonValue]:
        self.calls.append(request)
        return {"ok": True}


def as_user(url: str, user: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{user}:{PASSWORD}@{host}"))


@pytest.fixture
def executor_user() -> str:
    return f"overseer-executor-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def executor_redis(
    redis_client: Redis, test_redis_url: str, executor_user: str
) -> AsyncIterator[Redis]:
    user = executor_user
    await purge_executor_keys(redis_client)
    await redis_client.execute_command(
        "ACL", "SETUSER", user, "reset", "on", f">{PASSWORD}", *RULES.read_text().split()
    )
    client: Redis = from_url(as_user(test_redis_url, user), decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
        await redis_client.execute_command("ACL", "DELUSER", user)
        await purge_executor_keys(redis_client)


async def test_the_restricted_user_can_do_the_whole_executor_job(
    redis_client: Redis, executor_redis: Redis
) -> None:
    dispatcher = EchoDispatcher()
    lease = ExecutorLease(executor_redis, operations=OPERATIONS, ttl_s=1.0, interval_s=0.25)
    consumer = ExecutorConsumer(
        executor_redis,
        dispatcher,
        lease,
        reply_ttl_s=30,
        dedup_ttl_s=600.0,
        max_call_s=5.0,
        poll_timeout_s=0.1,
    )
    service = ExecutorService(consumer, lease, dispatcher)
    api = RedisExecutorClient(redis_client, pickup_timeout_s=1.0, poll_slice_s=0.2)
    request = write_request()

    task = asyncio.create_task(service.run())
    for _ in range(300):
        if lease.held:
            break
        await asyncio.sleep(0.01)
    response = await api.call(request)
    await asyncio.sleep(0.6)
    seen = await redis_client.exists(seen_key(request.id))
    service.request_stop()
    await asyncio.wait_for(task, 5)

    assert response.status == "ok"
    assert [call.id for call in dispatcher.calls] == [request.id]
    assert seen == 1
    assert await redis_client.exists("overseer:executor:owner", "overseer:executor:heartbeat") == 0


DENIED: list[Any] = [
    pytest.param(lambda r: r.rpush(REQUESTS_KEY, "forged"), id="push-to-the-request-queue"),
    pytest.param(lambda r: r.lpush(REQUESTS_KEY, "forged"), id="lpush-to-the-request-queue"),
    pytest.param(lambda r: r.lrem(REQUESTS_KEY, 1, "x"), id="lrem-the-request-queue"),
    pytest.param(lambda r: r.delete(REQUESTS_KEY), id="delete-the-request-queue"),
    pytest.param(lambda r: r.llen(REQUESTS_KEY), id="peek-the-request-queue"),
    pytest.param(lambda r: r.get("overseer:confirmation:pending:x"), id="read-a-confirmation"),
    pytest.param(lambda r: r.set("overseer:confirmation:claim:x", "1"), id="write-a-confirmation"),
    pytest.param(lambda r: r.delete("overseer:confirmation:pending:x"), id="drop-a-confirmation"),
    pytest.param(lambda r: r.zrange("arq:queue", 0, -1), id="read-the-arq-queue"),
    pytest.param(lambda r: r.get("arq:job:x"), id="read-an-arq-job"),
    pytest.param(lambda r: r.set("arq:queue", "x"), id="write-arq"),
    pytest.param(lambda r: r.get("conversation:x"), id="read-other-data"),
    pytest.param(lambda r: r.blpop(["arq:queue"], 0.1), id="blpop-elsewhere"),
    pytest.param(lambda r: r.rpush("overseer:other:list", "x"), id="rpush-elsewhere"),
    pytest.param(lambda r: r.keys("*"), id="keys"),
    pytest.param(lambda r: r.scan(0), id="scan"),
    pytest.param(lambda r: r.flushdb(), id="flushdb"),
    pytest.param(lambda r: r.flushall(), id="flushall"),
    pytest.param(lambda r: r.execute_command("ACL", "LIST"), id="acl-list"),
    pytest.param(lambda r: r.execute_command("ACL", "SETUSER", "x", "on"), id="acl-setuser"),
    pytest.param(lambda r: r.execute_command("ACL", "WHOAMI"), id="acl-whoami"),
    pytest.param(lambda r: r.config_get("requirepass"), id="config-get"),
    pytest.param(lambda r: r.config_set("appendonly", "no"), id="config-set"),
    pytest.param(lambda r: r.info(), id="info"),
    pytest.param(lambda r: r.execute_command("CLIENT", "LIST"), id="client-list"),
    pytest.param(lambda r: r.execute_command("CLIENT", "KILL", "ID", "1"), id="client-kill"),
    pytest.param(lambda r: r.publish("overseer", "x"), id="publish"),
    pytest.param(lambda r: r.execute_command("PUBSUB", "CHANNELS"), id="pubsub-channels"),
    pytest.param(lambda r: r.execute_command("SUBSCRIBE", "overseer"), id="subscribe"),
    pytest.param(lambda r: r.execute_command("PSUBSCRIBE", "*"), id="psubscribe"),
    pytest.param(lambda r: r.evalsha("0" * 40, 0), id="evalsha"),
    pytest.param(lambda r: r.script_load("return 1"), id="script-load"),
    pytest.param(lambda r: r.script_flush(), id="script-flush"),
    pytest.param(lambda r: r.execute_command("FUNCTION", "LIST"), id="function-list"),
    pytest.param(lambda r: r.execute_command("FCALL", "f", 0), id="fcall"),
    pytest.param(
        lambda r: r.eval("return redis.call('GET', KEYS[1])", 1, "overseer:confirmation:x"),
        id="eval-with-a-foreign-key",
    ),
    pytest.param(
        lambda r: r.eval("return redis.call('GET', 'overseer:confirmation:x')", 0),
        id="eval-reaching-out-from-the-script",
    ),
    pytest.param(
        lambda r: r.eval("return redis.call('RPUSH', 'overseer:executor:requests', 'x')", 0),
        id="eval-pushing-to-the-queue",
    ),
    pytest.param(
        lambda r: r.eval("return redis.call('FLUSHALL')", 0), id="eval-flushing-everything"
    ),
    pytest.param(lambda r: r.execute_command("MIGRATE", "h", 1, "k", 0, 1), id="migrate"),
    pytest.param(lambda r: r.dump("overseer:executor:heartbeat"), id="dump-own-key"),
]


@pytest.mark.parametrize("probe", DENIED)
async def test_everything_outside_the_executor_job_is_denied(
    executor_redis: Redis, probe: Probe
) -> None:
    with pytest.raises(ResponseError) as caught:
        await probe(executor_redis)

    assert isinstance(caught.value, NoPermissionError) or "ACL failure" in str(caught.value)


async def test_selecting_another_database_does_not_widen_the_key_patterns(
    executor_redis: Redis, redis_client: Redis
) -> None:
    await executor_redis.execute_command("SELECT", 0)

    with pytest.raises(NoPermissionError):
        await executor_redis.get("overseer:confirmation:pending:x")


async def test_the_reply_and_dedupe_keys_are_writable(executor_redis: Redis) -> None:
    request_id = uuid.uuid4()

    assert await rpush(executor_redis, reply_key(request_id), "x") == 1
    assert await executor_redis.expire(reply_key(request_id), 5)
    assert await executor_redis.set(seen_key(request_id), "1", nx=True, ex=5)
    assert await blpop(executor_redis, REQUESTS_KEY, 0.05) is None


@pytest.mark.parametrize(
    "command",
    [
        ["SHUTDOWN"],
        ["DEBUG", "SLEEP", "0"],
        ["MONITOR"],
        ["REPLICAOF", "127.0.0.1", "1"],
        ["MODULE", "LOAD", "/tmp/x.so"],
        ["CONFIG", "REWRITE"],
        ["SAVE"],
        ["BGSAVE"],
        ["FAILOVER"],
        ["SCRIPT", "KILL"],
        ["RPUSH", REQUESTS_KEY, "forged"],
    ],
    ids=lambda command: "-".join(command[:2]).lower(),
)
async def test_dangerous_commands_are_denied_by_a_dry_run(
    redis_client: Redis, executor_redis: Redis, executor_user: str, command: list[str]
) -> None:
    verdict = await redis_client.execute_command("ACL", "DRYRUN", executor_user, *command)

    assert verdict != "OK"
    assert "no permissions" in str(verdict).lower()


@pytest.mark.parametrize(
    "command",
    [
        ["BLPOP", REQUESTS_KEY, "1"],
        ["RPUSH", "overseer:executor:reply:1", "x"],
        ["EXPIRE", "overseer:executor:reply:1", "60"],
        ["SET", "overseer:executor:seen:1", "1", "NX", "PX", "600000"],
        ["EVAL", "return 1", "2", "overseer:executor:owner", "overseer:executor:heartbeat"],
        ["GET", "overseer:executor:heartbeat"],
    ],
    ids=lambda command: command[0].lower(),
)
async def test_the_commands_of_the_job_pass_a_dry_run(
    redis_client: Redis, executor_redis: Redis, executor_user: str, command: list[str]
) -> None:
    assert await redis_client.execute_command("ACL", "DRYRUN", executor_user, *command) == "OK"
