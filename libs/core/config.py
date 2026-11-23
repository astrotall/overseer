from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from libs.core.exceptions import ConfigurationError

BASE_DIR = Path(__file__).resolve().parents[2]

Env = Literal["local", "dev", "prod"]

LLMProvider = Literal["anthropic", "deepseek"]

API_TOKEN_MIN_LENGTH: Final[int] = 32
API_TOKEN_ALPHABET: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._~-]+")
ORIGIN_FORM: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9+.-]*://[^/?#\s@]+")
LOCAL_DEV_PASSWORD: Final[str] = "overseer"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Env = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+asyncpg://overseer:overseer@127.0.0.1:55432/overseer"
    redis_url: str = Field(
        default="redis://default:overseer@127.0.0.1:56379/0",
        description=(
            "Redis требует пароль (OVE-59): redis://<пользователь>:<пароль>@<хост>:<порт>/<база>. "
            "Пароль только из URL-безопасных символов: Arq не раскодирует %-последовательности, "
            "а redis-py раскодирует, и они разошлись бы."
        ),
    )
    database_url_test: str | None = Field(
        default=None,
        description=(
            "БД для тестов. Если не задана, тесты берут database_url и подставляют "
            "имя базы с суффиксом _test — рабочая база никогда не используется."
        ),
    )
    redis_url_test: str | None = Field(
        default=None,
        description=(
            "Redis для тестов. Если не задан, тесты берут redis_url и подставляют "
            "соседний номер логической базы — рабочий Redis никогда не используется."
        ),
    )

    llm_provider: LLMProvider = Field(
        default="deepseek",
        description="Активный LLM-провайдер: один на запущенный инстанс, без per-request "
        "переключения",
    )

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-5"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    api_host: str = Field(
        default="127.0.0.1",
        description=(
            "Адрес, на котором слушает uvicorn вне Docker. В контейнере CMD образа явно "
            "слушает 0.0.0.0, а наружу порт публикуется только на 127.0.0.1 (OVE-61)"
        ),
    )
    api_port: int = 8000
    api_token: SecretStr | None = Field(
        default=None,
        description=(
            "Bearer-токен API (OVE-61): обязателен, без него api не стартует. Не короче "
            f"{API_TOKEN_MIN_LENGTH} символов из A-Z a-z 0-9 . _ ~ - — он же едет в "
            "Sec-WebSocket-Protocol. Сгенерировать: openssl rand -hex 32"
        ),
    )
    api_allowed_origins: list[str] = Field(
        default_factory=list,
        description=(
            "Origin браузерных клиентов, которым разрешён /ws/chat, JSON-списком: "
            '["tauri://localhost"]. Пусто — ни одному браузерному origin нельзя; '
            "нативные клиенты Origin не шлют и проходят только по токену"
        ),
    )
    db_echo: bool = Field(default=False, description="Логировать SQL-запросы SQLAlchemy")

    browser_headless: bool = Field(
        default=True,
        description=(
            "Режим браузера Playwright. В Docker дисплея нет, headed там не запустится — "
            "false ставят только для локальной отладки вне контейнера."
        ),
    )
    browser_no_sandbox: bool = Field(
        default=False,
        description=(
            "Отключить песочницу Chromium. Аварийный выход для окружения, которое не даёт "
            "поднять пользовательские namespace'ы: без песочницы отрендеренная страница "
            "исполняется в том же процессном пространстве, что и сам агент."
        ),
    )
    browser_idle_ttl_seconds: int = Field(
        default=10 * 60,
        gt=0,
        description="Сколько браузерный контекст живёт без единого вызова, прежде чем его закроют",
    )
    browser_sweep_interval_seconds: int = Field(
        default=60,
        gt=0,
        description="Как часто сборщик проверяет контексты на простой",
    )
    browser_max_sessions: int = Field(
        default=4,
        gt=0,
        description="Потолок одновременно открытых браузерных контекстов на процесс",
    )

    executor_pickup_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Сколько api ждёт, пока executor возьмёт запрос из очереди (ответ accepted). "
            "Не взял — запрос снимается с очереди (LREM), и модель узнаёт, что действие "
            "не выполнено"
        ),
    )
    executor_call_budget_s: float = Field(
        default=70.0,
        gt=0,
        description=(
            "Потолок ожидания результата вызова executor. Больше таймаута COM-вызова "
            "контроллера (60 с) и не больше окна контракта (120 с)"
        ),
    )

    @field_validator("api_token", mode="before")
    @classmethod
    def _blank_token_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_token")
    @classmethod
    def _check_api_token_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        token = value.get_secret_value()
        if len(token) < API_TOKEN_MIN_LENGTH or not API_TOKEN_ALPHABET.fullmatch(token):
            raise ValueError(
                f"API_TOKEN должен быть не короче {API_TOKEN_MIN_LENGTH} символов из набора "
                "A-Z a-z 0-9 . _ ~ - (например, openssl rand -hex 32)"
            )
        return value

    @field_validator("api_allowed_origins")
    @classmethod
    def _check_allowed_origins(cls, value: list[str]) -> list[str]:
        origins = [origin.strip().lower() for origin in value]
        for origin in origins:
            if not ORIGIN_FORM.fullmatch(origin):
                raise ValueError(
                    "API_ALLOWED_ORIGINS: каждый элемент — точный origin вида "
                    "<схема>://<хост>[:<порт>] без пути; «*» и «null» не допускаются"
                )
        return origins

    @model_validator(mode="after")
    def _refuse_local_dev_secrets_in_prod(self) -> Self:
        if not self.is_prod:
            return self
        offenders = [
            name
            for name, url in (("DATABASE_URL", self.database_url), ("REDIS_URL", self.redis_url))
            if _url_password(url) == LOCAL_DEV_PASSWORD
        ]
        if offenders:
            raise ValueError(
                f"{', '.join(offenders)}: пароль по умолчанию для локальной разработки "
                "запрещён при ENV=prod — замени его (например, openssl rand -hex 32)"
            )
        return self

    @model_validator(mode="after")
    def _check_browser_sweep_interval(self) -> Self:
        if self.browser_sweep_interval_seconds > self.browser_idle_ttl_seconds:
            raise ValueError(
                "browser_sweep_interval_seconds не может превышать browser_idle_ttl_seconds: "
                "сборщик просыпался бы реже, чем истекает простой, и контекст жил бы дольше TTL"
            )
        return self

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


def _url_password(url: str) -> str | None:
    try:
        password = urlsplit(url).password
    except ValueError:
        return None
    return unquote(password) if password is not None else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


_LLM_PROVIDER_ENV_VAR: dict[LLMProvider, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def validate_llm_provider_key(settings: Settings) -> None:
    provider = settings.llm_provider
    key = settings.anthropic_api_key if provider == "anthropic" else settings.deepseek_api_key
    if key:
        return

    env_var = _LLM_PROVIDER_ENV_VAR[provider]
    raise ConfigurationError(f"{env_var} не задан — обязателен, так как LLM_PROVIDER={provider}")


def validate_api_token(settings: Settings) -> None:
    if settings.api_token is not None:
        return

    raise ConfigurationError(
        "API_TOKEN не задан — обязателен: каждый маршрут, кроме /health, требует этот токен, "
        "а отключить проверку нельзя. Сгенерировать: openssl rand -hex 32"
    )
