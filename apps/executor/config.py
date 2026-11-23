from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from apps.executor.excel.controller import CALL_TIMEOUT_S
from apps.executor.paths import local_path
from libs.core.config import LOCAL_DEV_PASSWORD, Env
from libs.core.exceptions import ConfigurationError
from libs.executor.client import DEFAULT_CALL_BUDGET_S
from libs.schemas.executor import MAX_CALL_WINDOW_S

BASE_DIR = Path(__file__).resolve().parents[2]

REDIS_SCHEMES: Final[frozenset[str]] = frozenset({"redis", "rediss"})
URL_SAFE_PASSWORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~-]+")
DEFAULT_REDIS_USER: Final = "default"

REDIS_URL_MISSING_TEXT: Final = (
    "EXECUTOR_REDIS_URL не задан: executor подключается к Redis отдельным пользователем, "
    "например redis://overseer-executor:<пароль>@127.0.0.1:56379/0 (пароль — "
    "REDIS_EXECUTOR_PASSWORD из .env)"
)


@dataclass(frozen=True, slots=True)
class RedisTarget:
    host: str
    port: int
    user: str
    db: int

    @property
    def restricted(self) -> bool:
        return self.user != DEFAULT_REDIS_USER


class ExecutorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="EXECUTOR_",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Env = Field(
        default="local",
        validation_alias=AliasChoices("EXECUTOR_ENV", "ENV"),
        description="Тот же смысл, что ENV у серверных процессов; EXECUTOR_ENV важнее ENV",
    )
    redis_url: SecretStr | None = Field(
        default=None,
        description=(
            "Redis со стороны хоста, отдельным ACL-пользователем: "
            "redis://overseer-executor:<пароль>@127.0.0.1:56379/0. Адрес — 127.0.0.1, а не "
            "localhost: порт опубликован только на IPv4-loopback. Пароль только из "
            "URL-безопасных символов. Значения по умолчанию нет"
        ),
    )
    heartbeat_interval_s: float = Field(
        default=5.0, gt=0, description="Как часто executor обновляет heartbeat и владение"
    )
    heartbeat_ttl_s: float = Field(
        default=15.0,
        gt=0,
        description="TTL heartbeat и ключа владения; не меньше двух интервалов",
    )
    reply_ttl_s: int = Field(
        default=60, ge=1, description="Сколько ответ ждёт в reply:<id>, если api уже ушёл"
    )
    dedup_ttl_s: float = Field(
        default=600.0,
        gt=0,
        description=f"Сколько помнится id запроса; больше окна вызова ({MAX_CALL_WINDOW_S:g} с)",
    )
    max_call_s: float = Field(
        default=DEFAULT_CALL_BUDGET_S,
        gt=0,
        le=MAX_CALL_WINDOW_S,
        description=(
            "Потолок одного вызова на стороне executor. Больше таймаута COM-вызова "
            f"контроллера ({CALL_TIMEOUT_S:g} с), чтобы типизированный timeout приходил первым"
        ),
    )
    allowed_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Папки, внутри которых executor открывает и сохраняет файлы, JSON-списком "
            "абсолютных путей Windows. Пусто — одна папка Documents\\Overseer в профиле "
            "пользователя, которую executor создаёт сам"
        ),
    )
    max_conversations: int = Field(
        default=4,
        ge=1,
        description="Сколько разговоров одновременно держат открытую книгу в Excel",
    )
    conversation_idle_ttl_s: float = Field(
        default=30 * 60.0,
        gt=0,
        description=(
            "Через сколько простоя книга разговора закрывается. Книга с несохранёнными "
            "изменениями не закрывается никогда — она остаётся открытой и попадает в лог"
        ),
    )
    log_level: str = "INFO"

    @field_validator("redis_url", mode="before")
    @classmethod
    def _blank_url_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        parts = _split(value.get_secret_value())
        if parts is None or parts.scheme not in REDIS_SCHEMES or not parts.hostname:
            raise ValueError(
                "EXECUTOR_REDIS_URL: ожидается redis://<пользователь>:<пароль>@<хост>:<порт>/<база>"
            )
        password = parts.password
        if password is not None and not URL_SAFE_PASSWORD.fullmatch(password):
            raise ValueError(
                "EXECUTOR_REDIS_URL: пароль только из символов A-Z a-z 0-9 . _ ~ - — "
                "redis-py и Arq по-разному раскодируют %-последовательности"
            )
        return value

    @field_validator("allowed_roots")
    @classmethod
    def _check_allowed_roots(cls, value: list[str]) -> list[str]:
        for index, root in enumerate(value):
            if local_path(root) is None:
                raise ValueError(
                    f"EXECUTOR_ALLOWED_ROOTS: элемент {index} — не локальная папка вида C:\\...: "
                    "сетевые, device- и относительные пути, «..», имена устройств и "
                    "альтернативные потоки запрещены"
                )
        return value

    @model_validator(mode="after")
    def _check_timings(self) -> Self:
        if self.heartbeat_ttl_s < 2 * self.heartbeat_interval_s:
            raise ValueError(
                "EXECUTOR_HEARTBEAT_TTL_S должен быть не меньше двух "
                "EXECUTOR_HEARTBEAT_INTERVAL_S: иначе одно пропущенное обновление снимает "
                "executor с очереди"
            )
        if self.dedup_ttl_s <= MAX_CALL_WINDOW_S:
            raise ValueError(
                f"EXECUTOR_DEDUP_TTL_S должен быть больше окна вызова ({MAX_CALL_WINDOW_S:g} с): "
                "иначе повтор после истечения дедупликации ещё не просрочен"
            )
        if self.max_call_s <= CALL_TIMEOUT_S:
            raise ValueError(
                f"EXECUTOR_MAX_CALL_S должен быть больше таймаута COM-вызова ({CALL_TIMEOUT_S:g} с)"
            )
        return self

    @model_validator(mode="after")
    def _refuse_weak_redis_access_in_prod(self) -> Self:
        if self.env != "prod" or self.redis_url is None:
            return self
        parts = _split(self.redis_url.get_secret_value())
        if parts is None:
            return self
        if (parts.username or DEFAULT_REDIS_USER) == DEFAULT_REDIS_USER:
            raise ValueError(
                "EXECUTOR_REDIS_URL: при ENV=prod executor ходит в Redis только отдельным "
                "ACL-пользователем (overseer-executor), а не пользователем default"
            )
        if parts.password == LOCAL_DEV_PASSWORD:
            raise ValueError(
                "EXECUTOR_REDIS_URL: пароль по умолчанию для локальной разработки запрещён "
                "при ENV=prod — задай REDIS_EXECUTOR_PASSWORD (openssl rand -hex 32)"
            )
        return self


def _split(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    return parts


def require_redis_url(settings: ExecutorSettings) -> str:
    if settings.redis_url is None:
        raise ConfigurationError(REDIS_URL_MISSING_TEXT)
    return settings.redis_url.get_secret_value()


def redis_target(settings: ExecutorSettings) -> RedisTarget:
    parts = _split(require_redis_url(settings))
    if parts is None or parts.hostname is None:
        raise ConfigurationError(REDIS_URL_MISSING_TEXT)
    db = parts.path.lstrip("/") or "0"
    return RedisTarget(
        host=parts.hostname,
        port=parts.port or 6379,
        user=parts.username or DEFAULT_REDIS_USER,
        db=int(db) if db.isdigit() else 0,
    )


@lru_cache(maxsize=1)
def get_executor_settings() -> ExecutorSettings:
    return ExecutorSettings()
