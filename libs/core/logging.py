from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from libs.core.config import Settings, get_settings


def configure_logging(settings: Settings | None = None, *, level: str | None = None) -> None:
    settings = settings or get_settings()
    level_name = (level or settings.log_level).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
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
