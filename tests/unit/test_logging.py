from __future__ import annotations

import logging

import pytest

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
