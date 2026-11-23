from __future__ import annotations

import logging
import re
import sys
from typing import Any, Final

import structlog

from libs.core.config import Settings, get_settings

REDACTED: Final[str] = "***"
SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(overseer\.token\.)[A-Za-z0-9._~-]+"),
    re.compile(r"(?i)(rediss?://[^:/@\s]*:)[^@\s]+(?=@)"),
    re.compile(r"(?i)(\b(?:REDIS_EXECUTOR_PASSWORD|REDIS_PASSWORD)\s*[=:]\s*[\"']?)[^\s\"',]+"),
    re.compile(r"(?i)(\buser\s+\S+\s+(?:\S+\s+)*?>)[^\s]+"),
)
HEADER_DUMPING_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn.error",
    "uvicorn.asgi",
    "websockets.client",
    "websockets.server",
)


def redact_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(rf"\g<1>{REDACTED}", text)
    return text


def redact_event_secrets(
    logger: object, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact_secrets(value)
    return event_dict


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def install_secret_redaction() -> None:
    for name in HEADER_DUMPING_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, SecretRedactingFilter) for existing in logger.filters):
            logger.addFilter(SecretRedactingFilter())


def configure_logging(settings: Settings | None = None, *, level: str | None = None) -> None:
    settings = settings or get_settings()
    level_name = (level or settings.log_level).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)
    install_secret_redaction()

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_event_secrets,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if (settings.log_json or settings.is_prod)
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
