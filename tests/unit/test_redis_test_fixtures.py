from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.exceptions import ResponseError

from libs.core.config import Settings
from tests.conftest import (
    RedisClusterModeNotSupportedError,
    _raise_if_cluster_mode_select_error,
    redis_client,
)

CLUSTER_MODE_ERROR = ResponseError("ERR SELECT is not allowed in cluster mode")


def test_derived_db_against_cluster_mode_redis_raises_clear_error() -> None:
    with pytest.raises(RedisClusterModeNotSupportedError, match="REDIS_URL_TEST must be set"):
        _raise_if_cluster_mode_select_error(CLUSTER_MODE_ERROR, redis_url_test=None)


def test_explicit_redis_url_test_skips_the_cluster_mode_check() -> None:
    _raise_if_cluster_mode_select_error(
        CLUSTER_MODE_ERROR, redis_url_test="redis://cluster-node:6379/0"
    )


def test_unrelated_response_error_is_not_mistaken_for_cluster_mode() -> None:
    _raise_if_cluster_mode_select_error(
        ResponseError("WRONGPASS invalid username-password pair"), redis_url_test=None
    )


class _FakeClusterModeRedis:
    async def ping(self) -> None:
        raise CLUSTER_MODE_ERROR

    async def aclose(self) -> None:
        pass


async def test_redis_client_fixture_fails_clearly_against_cluster_mode_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tests.conftest.from_url", lambda *_args, **_kwargs: _FakeClusterModeRedis()
    )
    settings = Settings(redis_url_test=None)

    fixture_func = redis_client.__wrapped__  # type: ignore[attr-defined]
    generator: AsyncIterator[Any] = fixture_func("redis://irrelevant:6379/1", settings)

    with pytest.raises(RedisClusterModeNotSupportedError, match="REDIS_URL_TEST must be set"):
        await anext(generator)
