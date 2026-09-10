from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from libs.core.exceptions import ConfigurationError

BASE_DIR = Path(__file__).resolve().parents[2]

Env = Literal["local", "dev", "prod"]

LLMProvider = Literal["anthropic", "deepseek"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: Env = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+asyncpg://overseer:overseer@localhost:55432/overseer"
    redis_url: str = "redis://localhost:56379/0"
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

    api_host: str = "0.0.0.0"
    api_port: int = 8000
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
