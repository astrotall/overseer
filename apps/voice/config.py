from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="VOICE_",
        case_sensitive=False,
        extra="ignore",
    )

    wake_word_phrase: str = Field(
        default="hey jarvis",
        description=(
            "Фраза активации. Без VOICE_WAKE_WORD_MODEL_PATH берётся готовая модель "
            "openWakeWord с таким именем — список в apps/voice/wake_word.py"
        ),
    )
    wake_word_model_path: Path | None = Field(
        default=None,
        description=(
            "Путь к своей обученной модели openWakeWord (.onnx). Задан — фраза берётся "
            "из него, а wake_word_phrase остаётся только меткой для логов"
        ),
    )
    wake_word_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Порог срабатывания детектора: ниже — больше ложных срабатываний, выше — "
            "чаще не слышит. Единственная ручка калибровки, крутится по месту"
        ),
    )
    input_device: str | None = Field(
        default=None,
        description=(
            "Устройство ввода для sounddevice: индекс или часть имени. None — системное "
            "по умолчанию"
        ),
    )
    log_level: str = "INFO"

    @field_validator("wake_word_model_path", "input_device", mode="before")
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None

        return value


@lru_cache(maxsize=1)
def get_voice_settings() -> VoiceSettings:
    return VoiceSettings()
