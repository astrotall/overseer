from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from apps.executor.config import (
    REDIS_URL_MISSING_TEXT,
    ExecutorSettings,
    get_executor_settings,
    redis_target,
    require_redis_url,
)
from libs.core.exceptions import ConfigurationError

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
STRONG_PASSWORD = "0123456789abcdef0123456789abcdef"
EXECUTOR_URL = f"redis://overseer-executor:{STRONG_PASSWORD}@127.0.0.1:56379/0"


def settings(**values: Any) -> ExecutorSettings:
    return ExecutorSettings.model_validate(values)


def test_the_defaults_follow_the_ove_29_design() -> None:
    defaults = ExecutorSettings()

    assert defaults.redis_url is None
    assert defaults.heartbeat_interval_s == 5.0
    assert defaults.heartbeat_ttl_s == 15.0
    assert defaults.reply_ttl_s == 60
    assert defaults.dedup_ttl_s == 600.0
    assert defaults.max_call_s == 70.0
    assert defaults.allowed_roots == []
    assert defaults.env == "local"


def test_the_environment_and_dotenv_of_this_machine_do_not_leak_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ExecutorSettings.model_config.get("env_file") is None
    assert get_executor_settings().redis_url is None

    monkeypatch.setenv("EXECUTOR_REDIS_URL", EXECUTOR_URL)
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_INTERVAL_S", "2.5")
    get_executor_settings.cache_clear()

    loaded = get_executor_settings()
    assert require_redis_url(loaded) == EXECUTOR_URL
    assert loaded.heartbeat_interval_s == 2.5


@pytest.mark.parametrize("name", ["EXECUTOR_ENV", "ENV"])
def test_the_environment_name_is_read_from_either_variable(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "dev")

    assert ExecutorSettings().env == "dev"


def test_every_setting_has_a_key_in_the_env_example() -> None:
    keys = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    expected = {f"EXECUTOR_{name.upper()}" for name in ExecutorSettings.model_fields} - {
        "EXECUTOR_ENV"
    }

    assert expected <= keys
    assert "ENV" in keys


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"heartbeat_interval_s": 5.0, "heartbeat_ttl_s": 9.0}, id="ttl-too-short"),
        pytest.param({"dedup_ttl_s": 120.0}, id="dedup-not-beyond-window"),
        pytest.param({"max_call_s": 60.0}, id="cap-not-beyond-com-timeout"),
        pytest.param({"max_call_s": 121.0}, id="cap-beyond-window"),
        pytest.param({"redis_url": "http://127.0.0.1:56379/0"}, id="scheme"),
        pytest.param({"redis_url": "redis://:pw@/0"}, id="no-host"),
        pytest.param({"redis_url": "redis://u:p%40ss@127.0.0.1:1/0"}, id="encoded-password"),
        pytest.param({"redis_url": "redis://u:p@ss@127.0.0.1:1/0"}, id="unsafe-password"),
    ],
)
def test_impossible_configurations_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        settings(**values)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"redis://default:{STRONG_PASSWORD}@127.0.0.1:56379/0", id="default-user"),
        pytest.param(f"redis://:{STRONG_PASSWORD}@127.0.0.1:56379/0", id="implicit-default"),
        pytest.param("redis://overseer-executor:overseer@127.0.0.1:56379/0", id="dev-password"),
    ],
)
def test_prod_refuses_weak_redis_access(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        settings(env="prod", redis_url=url)

    assert "EXECUTOR_REDIS_URL" in str(caught.value)


def test_prod_accepts_the_restricted_user_with_a_real_password() -> None:
    assert settings(env="prod", redis_url=EXECUTOR_URL).env == "prod"


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"redis_url": "redis://u:p@ss-SECRET@127.0.0.1:1/0"}, id="field"),
        pytest.param({"env": "prod", "redis_url": "redis://default:SECRET-x@h:1/0"}, id="model"),
    ],
)
def test_errors_never_repeat_the_url(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        settings(**values)

    assert "SECRET" not in str(caught.value)


def test_a_missing_url_is_a_clear_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as caught:
        require_redis_url(ExecutorSettings())

    assert str(caught.value) == REDIS_URL_MISSING_TEXT


def test_the_target_describes_the_connection_without_the_password() -> None:
    target = redis_target(settings(redis_url=EXECUTOR_URL))

    assert (target.host, target.port, target.user, target.db) == (
        "127.0.0.1",
        56379,
        "overseer-executor",
        0,
    )
    assert target.restricted
    assert STRONG_PASSWORD not in repr(target)
    assert not redis_target(
        settings(redis_url=f"redis://:{STRONG_PASSWORD}@127.0.0.1:1/0")
    ).restricted


def test_the_url_is_a_secret_in_reprs() -> None:
    assert STRONG_PASSWORD not in repr(settings(redis_url=EXECUTOR_URL))
