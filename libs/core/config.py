from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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

    llm_provider: LLMProvider = Field(
        default="deepseek",
        description="Активный LLM-провайдер: один на запущенный инстанс, без per-request "
        "переключения",
    )

    anthropic_api_key: str | None = None
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    db_echo: bool = Field(default=False, description="Логировать SQL-запросы SQLAlchemy")

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
