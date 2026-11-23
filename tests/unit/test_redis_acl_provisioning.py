from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from libs.executor.keys import (
    EXECUTOR_REDIS_USER,
    HEARTBEAT_KEY,
    NAMESPACE,
    OWNER_KEY,
    REPLY_PREFIX,
    REQUESTS_KEY,
    SEEN_PREFIX,
)

DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"
ENTRYPOINT = DOCKER_DIR / "redis" / "entrypoint.sh"
RULES = DOCKER_DIR / "redis" / "executor.acl"
COMPOSE = DOCKER_DIR / "docker-compose.yml"

DEFAULT_PASSWORD = "default-password-not-a-secret"
EXECUTOR_PASSWORD = "0123456789abcdef0123456789abcdef"
SHARED_PASSWORD = "shared-password-for-both-users-0123456789"

STUB = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_ARGS"
cat > "$STUB_CONFIG"
"""


@dataclass(frozen=True)
class Rendered:
    returncode: int
    stderr: str
    args: list[str] | None
    config: str | None


def render(tmp_path: Path, **env: str) -> Rendered:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker-entrypoint.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    args_file, config_file = tmp_path / "args", tmp_path / "config"
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "--appendonly", "yes"],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_ARGS": str(args_file),
            "STUB_CONFIG": str(config_file),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return Rendered(
        returncode=result.returncode,
        stderr=result.stderr,
        args=args_file.read_text().splitlines() if args_file.exists() else None,
        config=config_file.read_text() if config_file.exists() else None,
    )


def test_without_an_executor_password_only_the_default_user_is_configured(
    tmp_path: Path,
) -> None:
    rendered = render(tmp_path, REDIS_PASSWORD=DEFAULT_PASSWORD)

    assert rendered.returncode == 0, rendered.stderr
    assert rendered.args == ["redis-server", "-", "--appendonly", "yes"]
    assert rendered.config is not None
    assert f"requirepass {DEFAULT_PASSWORD}" in rendered.config.splitlines()
    assert "user " not in rendered.config


def test_an_empty_executor_password_counts_as_unset(tmp_path: Path) -> None:
    rendered = render(tmp_path, REDIS_PASSWORD=DEFAULT_PASSWORD, REDIS_EXECUTOR_PASSWORD="")

    assert rendered.returncode == 0, rendered.stderr
    assert rendered.config is not None
    assert "user " not in rendered.config


def test_the_executor_user_is_configured_on_stdin_never_on_the_command_line(
    tmp_path: Path,
) -> None:
    rendered = render(
        tmp_path, REDIS_PASSWORD=DEFAULT_PASSWORD, REDIS_EXECUTOR_PASSWORD=EXECUTOR_PASSWORD
    )

    assert rendered.returncode == 0, rendered.stderr
    assert rendered.args is not None
    assert not any(EXECUTOR_PASSWORD in arg or DEFAULT_PASSWORD in arg for arg in rendered.args)
    assert rendered.config is not None
    (user_line,) = [line for line in rendered.config.splitlines() if line.startswith("user ")]
    assert user_line.startswith(f"user {EXECUTOR_REDIS_USER} reset on >{EXECUTOR_PASSWORD} ")
    assert user_line.split(f">{EXECUTOR_PASSWORD} ", 1)[1].split() == RULES.read_text().split()


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("short-but-safe", id="too-short"),
        pytest.param(EXECUTOR_PASSWORD + " +@all ~*", id="rule-injection"),
        pytest.param(EXECUTOR_PASSWORD + "\nuser evil on nopass +@all ~*", id="line-injection"),
        pytest.param(EXECUTOR_PASSWORD + "%40", id="not-url-safe"),
        pytest.param(SHARED_PASSWORD, id="same-as-default"),
    ],
)
def test_an_unusable_executor_password_stops_redis_without_echoing_it(
    tmp_path: Path, password: str
) -> None:
    default = SHARED_PASSWORD if password == SHARED_PASSWORD else DEFAULT_PASSWORD
    rendered = render(tmp_path, REDIS_PASSWORD=default, REDIS_EXECUTOR_PASSWORD=password)

    assert rendered.returncode != 0
    assert rendered.args is None
    assert password not in rendered.stderr
    assert "REDIS_EXECUTOR_PASSWORD" in rendered.stderr


def test_a_missing_default_password_stops_redis(tmp_path: Path) -> None:
    rendered = render(tmp_path)

    assert rendered.returncode != 0
    assert rendered.args is None


def redis_service() -> str:
    text = COMPOSE.read_text()
    match = re.search(r"^  redis:\n(?P<body>(?:    .*\n|\n)+?)^  \S", text, re.MULTILINE)
    assert match is not None
    return match.group("body")


def test_compose_runs_redis_through_the_mounted_entrypoint() -> None:
    service = redis_service()

    assert 'command: ["sh", "/usr/local/etc/overseer/entrypoint.sh"' in service
    assert "- ./redis:/usr/local/etc/overseer:ro" in service
    assert "REDIS_EXECUTOR_PASSWORD: ${REDIS_EXECUTOR_PASSWORD:-}" in service
    assert 'REDISCLI_AUTH=\\"$$REDIS_PASSWORD\\" redis-cli ping | grep -qx PONG' in service


def test_no_executor_password_default_exists_anywhere() -> None:
    sources = [COMPOSE, ENTRYPOINT, Path(__file__).resolve().parents[2] / ".env.example"]
    for source in sources:
        for line in source.read_text().splitlines():
            if "REDIS_EXECUTOR_PASSWORD" in line and not line.lstrip().startswith("#"):
                assert re.search(r"REDIS_EXECUTOR_PASSWORD(=|:-)[^}\s]", line) is None, line


def rule_tokens() -> list[str]:
    return RULES.read_text().replace("(", " ").replace(")", " ").split()


def test_every_key_pattern_stays_inside_the_executor_namespace() -> None:
    patterns = {token[1:] for token in rule_tokens() if token.startswith("~")}

    assert patterns == {
        REQUESTS_KEY,
        HEARTBEAT_KEY,
        OWNER_KEY,
        f"{REPLY_PREFIX}*",
        f"{SEEN_PREFIX}*",
    }
    assert all(pattern.startswith(NAMESPACE) for pattern in patterns)
    assert not any(
        token.startswith(("%", "&", "allkeys", "allchannels")) for token in rule_tokens()
    )


def test_the_rules_grant_named_commands_only() -> None:
    granted = {token for token in rule_tokens() if token.startswith("+")}

    assert not any(token.startswith("+@") for token in granted)
    assert granted == {
        "+ping",
        "+select",
        "+client|setinfo",
        "+blpop",
        "+rpush",
        "+expire",
        "+get",
        "+set",
        "+del",
        "+eval",
    }
