from __future__ import annotations

import logging
from typing import Any

import pytest
import structlog

from libs.core import logging as overseer_logging
from libs.core.logging import (
    HEADER_DUMPING_LOGGERS,
    SecretRedactingFilter,
    install_secret_redaction,
    redact_secrets,
)

TOKEN = "overseer-log-token-not-a-secret-0123456789"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"> Authorization: Bearer {TOKEN}", "> Authorization: Bearer ***"),
        (f"(b'authorization', b'bearer {TOKEN}')", "(b'authorization', b'bearer ***')"),
        (
            f"< sec-websocket-protocol: overseer.v1, overseer.token.{TOKEN}",
            "< sec-websocket-protocol: overseer.v1, overseer.token.***",
        ),
        ("< GET /ws/chat HTTP/1.1", "< GET /ws/chat HTTP/1.1"),
    ],
)
def test_secrets_are_redacted_from_log_text(text: str, expected: str) -> None:
    assert redact_secrets(text) == expected


def test_the_filter_is_installed_once_on_every_logger_that_dumps_headers() -> None:
    install_secret_redaction()
    install_secret_redaction()

    for name in HEADER_DUMPING_LOGGERS:
        filters = logging.getLogger(name).filters
        assert sum(isinstance(item, SecretRedactingFilter) for item in filters) == 1


def test_configure_logging_installs_the_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []
    monkeypatch.setattr(overseer_logging, "install_secret_redaction", lambda: calls.append(None))
    monkeypatch.setattr(overseer_logging.structlog, "configure", lambda **_kwargs: None)

    overseer_logging.configure_logging(level="INFO")

    assert calls == [None]


@pytest.mark.parametrize("name", HEADER_DUMPING_LOGGERS)
def test_a_handshake_dump_reaches_the_log_without_the_token(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    install_secret_redaction()
    caplog.set_level(logging.DEBUG, logger=name)

    logging.getLogger(name).debug("> %s: %s", "Sec-WebSocket-Protocol", f"overseer.token.{TOKEN}")
    logging.getLogger(name).debug("> %s: %s", "Authorization", f"Bearer {TOKEN}")

    assert caplog.records
    assert TOKEN not in caplog.text


EXECUTOR_PASSWORD = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "text",
    [
        f"redis://overseer-executor:{EXECUTOR_PASSWORD}@127.0.0.1:56379/0",
        f"rediss://:{EXECUTOR_PASSWORD}@redis.internal:6380/1",
        f"REDIS_EXECUTOR_PASSWORD={EXECUTOR_PASSWORD}",
        f"redis_executor_password: '{EXECUTOR_PASSWORD}'",
        f"REDIS_PASSWORD={EXECUTOR_PASSWORD} REDIS_PORT=56379",
        f"user overseer-executor reset on >{EXECUTOR_PASSWORD} +ping",
    ],
)
def test_redis_passwords_are_redacted_from_log_text(text: str) -> None:
    redacted = redact_secrets(text)

    assert EXECUTOR_PASSWORD not in redacted
    assert "***" in redacted


def test_redis_urls_without_a_password_are_left_alone() -> None:
    text = "redis://127.0.0.1:56379/0 and redis://overseer-executor@127.0.0.1:56379/0"

    assert redact_secrets(text) == text


def test_structured_events_are_redacted_too() -> None:
    event = overseer_logging.redact_event_secrets(
        None,
        "error",
        {
            "event": "executor.stopped",
            "reason": f"bad url redis://overseer-executor:{EXECUTOR_PASSWORD}@127.0.0.1:1/0",
            "exception": f"Traceback ...\nREDIS_EXECUTOR_PASSWORD={EXECUTOR_PASSWORD}",
            "attempt": 3,
        },
    )

    assert EXECUTOR_PASSWORD not in str(event)
    assert event["attempt"] == 3


def test_configure_logging_redacts_structured_events_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: dict[str, Any] = {}
    monkeypatch.setattr(overseer_logging, "install_secret_redaction", lambda: None)
    monkeypatch.setattr(
        overseer_logging.structlog, "configure", lambda **kwargs: configured.update(kwargs)
    )

    overseer_logging.configure_logging(level="INFO")

    processors = configured["processors"]
    assert overseer_logging.redact_event_secrets in processors
    assert processors.index(overseer_logging.redact_event_secrets) == len(processors) - 2
    assert processors.index(overseer_logging.redact_event_secrets) > processors.index(
        structlog.processors.format_exc_info
    )
