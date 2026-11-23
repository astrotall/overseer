from __future__ import annotations

from redis.asyncio import Redis


async def blpop(redis: Redis, key: str, block_s: float) -> str | None:
    popped = await redis.execute_command("BLPOP", key, block_s)
    if not popped:
        return None
    _, value = popped
    return str(value)


async def rpush(redis: Redis, key: str, value: str) -> int:
    return int(await redis.execute_command("RPUSH", key, value))


async def lrem(redis: Redis, key: str, value: str) -> int:
    return int(await redis.execute_command("LREM", key, 1, value))


async def eval_script(redis: Redis, script: str, keys: list[str], args: list[str]) -> object:
    result: object = await redis.execute_command("EVAL", script, len(keys), *keys, *args)
    return result
