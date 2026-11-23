from __future__ import annotations

import pytest
from arq.connections import RedisSettings
from pydantic import ValidationError
from redis.asyncio.connection import parse_url

from libs.core.config import Settings, validate_api_token, validate_llm_provider_key
from libs.core.exceptions import ConfigurationError


def test_validate_llm_provider_key_passes_when_active_provider_key_is_set() -> None:
    settings = Settings(
        llm_provider="deepseek", deepseek_api_key="test-key", anthropic_api_key=None
    )

    validate_llm_provider_key(settings)


def test_validate_llm_provider_key_raises_when_deepseek_key_missing() -> None:
    settings = Settings(llm_provider="deepseek", deepseek_api_key=None)

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        validate_llm_provider_key(settings)


def test_validate_llm_provider_key_raises_when_anthropic_key_missing() -> None:
    settings = Settings(llm_provider="anthropic", anthropic_api_key=None)

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        validate_llm_provider_key(settings)


def test_validate_llm_provider_key_passes_when_anthropic_key_is_set() -> None:
    settings = Settings(
        llm_provider="anthropic", anthropic_api_key="test-key", deepseek_api_key=None
    )

    validate_llm_provider_key(settings)


def test_arq_and_redis_py_read_the_default_redis_url_the_same_way() -> None:
    default = Settings.model_fields["redis_url"].default
    arq = RedisSettings.from_dsn(default)

    assert parse_url(default) == {
        "username": arq.username,
        "password": arq.password,
        "host": arq.host,
        "port": arq.port,
        "db": arq.database,
    }
    assert arq.password
    assert arq.host == "127.0.0.1"


GOOD_TOKEN = "a" * 64
STRONG_DATABASE_URL = "postgresql+asyncpg://overseer:9f1c2e7a5b@db.internal:5432/overseer"
STRONG_REDIS_URL = "redis://default:4b8d0e6f2a@redis.internal:6379/0"
DEFAULT_DATABASE_URL = Settings.model_fields["database_url"].default
DEFAULT_REDIS_URL = Settings.model_fields["redis_url"].default


def isolated_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_validate_api_token_passes_when_the_token_is_set() -> None:
    validate_api_token(isolated_settings(api_token=GOOD_TOKEN))


@pytest.mark.parametrize("token", [None, "", "   "])
def test_validate_api_token_raises_when_the_token_is_missing(token: str | None) -> None:
    settings = isolated_settings(api_token=token)

    assert settings.api_token is None
    with pytest.raises(ConfigurationError, match="API_TOKEN"):
        validate_api_token(settings)


@pytest.mark.parametrize(
    "token",
    ["short-secret-value", "a" * 31, "b" * 40 + "/", "c" * 40 + " d", "ключ" * 10, "e" * 40 + ","],
)
def test_a_malformed_token_is_rejected_without_echoing_it(token: str) -> None:
    with pytest.raises(ValidationError, match="API_TOKEN") as failure:
        isolated_settings(api_token=token)

    assert token not in str(failure.value)
    assert token not in repr(failure.value)


def test_the_token_is_hidden_from_the_settings_repr() -> None:
    settings = isolated_settings(api_token=GOOD_TOKEN)

    assert GOOD_TOKEN not in repr(settings)
    assert GOOD_TOKEN not in str(settings.model_dump())


def test_allowed_origins_default_to_none() -> None:
    assert isolated_settings().api_allowed_origins == []


def test_allowed_origins_are_read_as_a_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ALLOWED_ORIGINS", '["tauri://localhost", "HTTP://127.0.0.1:1420"]')

    assert isolated_settings().api_allowed_origins == ["tauri://localhost", "http://127.0.0.1:1420"]


@pytest.mark.parametrize(
    "origin",
    ["*", "null", "https://*.example.com/", "https://example.com/app", "example.com", ""],
)
def test_a_wildcard_or_malformed_origin_is_rejected(origin: str) -> None:
    with pytest.raises(ValidationError, match="API_ALLOWED_ORIGINS"):
        isolated_settings(api_allowed_origins=[origin])


@pytest.mark.parametrize("env", ["local", "dev"])
def test_local_dev_defaults_are_accepted_outside_prod(env: str) -> None:
    settings = isolated_settings(env=env)

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.redis_url == DEFAULT_REDIS_URL


@pytest.mark.parametrize(
    ("values", "named", "not_named"),
    [
        ({"redis_url": STRONG_REDIS_URL}, "DATABASE_URL", "REDIS_URL"),
        ({"database_url": STRONG_DATABASE_URL}, "REDIS_URL", "DATABASE_URL"),
        ({}, "DATABASE_URL, REDIS_URL", None),
        (
            {
                "database_url": "postgresql+asyncpg://someone:overseer@db:5432/app",
                "redis_url": "redis://default:%6Fverseer@redis:6379/0",
            },
            "DATABASE_URL, REDIS_URL",
            None,
        ),
    ],
    ids=["database", "redis", "both", "renamed-user-and-encoded"],
)
def test_local_dev_passwords_are_refused_in_prod(
    values: dict[str, str], named: str, not_named: str | None
) -> None:
    with pytest.raises(ValidationError) as failure:
        isolated_settings(env="prod", **values)

    message = str(failure.value)
    assert named in message
    assert "ENV=prod" in message
    if not_named is not None:
        assert f"{not_named}:" not in message
    assert "overseer@" not in message
    assert ":overseer" not in message
    assert "%6Fverseer" not in message
    for value in values.values():
        assert value not in message


def test_non_default_secrets_are_accepted_in_prod() -> None:
    settings = isolated_settings(
        env="prod", database_url=STRONG_DATABASE_URL, redis_url=STRONG_REDIS_URL
    )

    assert settings.is_prod


def test_no_settings_error_ever_shows_the_inputs() -> None:
    with pytest.raises(ValidationError) as failure:
        isolated_settings(
            database_url=STRONG_DATABASE_URL,
            browser_idle_ttl_seconds=10,
            browser_sweep_interval_seconds=60,
        )

    assert "browser_sweep_interval_seconds" in str(failure.value)
    assert "9f1c2e7a5b" not in str(failure.value)
