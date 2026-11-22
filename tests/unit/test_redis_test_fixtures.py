from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from redis.exceptions import AuthenticationError, ResponseError

from libs.core.config import Settings
from tests.conftest import (
    RedisClusterModeNotSupportedError,
    _raise_if_cluster_mode_select_error,
    redact_redis_url,
    redis_client,
    test_redis_url,
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


@pytest.mark.parametrize(
    ("redis_url", "expected"),
    [
        ("redis://default:pw@127.0.0.1:56379/0", "redis://default:pw@127.0.0.1:56379/1"),
        ("redis://overseer-api:pw@127.0.0.1:56379/15", "redis://overseer-api:pw@127.0.0.1:56379/0"),
        ("redis://:pw@redis:6379", "redis://:pw@redis:6379/1"),
    ],
)
def test_the_derived_test_url_keeps_the_credentials(redis_url: str, expected: str) -> None:
    settings = Settings(redis_url=redis_url, redis_url_test=None)

    fixture_func = test_redis_url.__wrapped__  # type: ignore[attr-defined]

    assert fixture_func(settings) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://default:s3cret@127.0.0.1:56379/1", "redis://default:***@127.0.0.1:56379/1"),
        ("redis://:s3cret@redis:6379/0", "redis://:***@redis:6379/0"),
        ("redis://127.0.0.1:6379/1", "redis://127.0.0.1:6379/1"),
    ],
)
def test_the_password_is_redacted_from_the_shown_url(url: str, expected: str) -> None:
    assert redact_redis_url(url) == expected


class _FakeUnauthenticatedRedis:
    async def ping(self) -> None:
        raise AuthenticationError("Authentication required.")

    async def aclose(self) -> None:
        pass


async def test_rejected_credentials_fail_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tests.conftest.from_url", lambda *_args, **_kwargs: _FakeUnauthenticatedRedis()
    )
    settings = Settings(redis_url_test=None)

    fixture_func = redis_client.__wrapped__  # type: ignore[attr-defined]
    generator: AsyncIterator[Any] = fixture_func("redis://default:s3cret@127.0.0.1:1/1", settings)

    with pytest.raises(pytest.fail.Exception, match="REDIS_PASSWORD") as failure:
        await anext(generator)
    assert "s3cret" not in str(failure.value)
