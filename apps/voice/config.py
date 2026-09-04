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
    stt_model: str = Field(
        default="small",
        description=(
            "Модель faster-whisper: tiny/base/small/medium/large-v3 или путь к своей. "
            "Крупнее — точнее и медленнее; small — компромисс для CPU"
        ),
    )
    stt_device: str = Field(
        default="auto",
        description="Устройство инференса CTranslate2: auto, cpu или cuda",
    )
    stt_compute_type: str = Field(
        default="int8",
        description="Квантизация CTranslate2: int8, int8_float16, float16, float32",
    )
    stt_language: str | None = Field(
        default="ru",
        description=(
            "Язык распознавания по ISO 639-1. Пусто — автоопределение, ненадёжное на "
            "коротких репликах"
        ),
    )
    vad_speech_rms: float = Field(
        default=300.0,
        gt=0.0,
        description=(
            "Порог речи по RMS кадра (шкала int16): выше — конец реплики находится "
            "раньше и шумная комната не мешает, ниже — слышно тихую речь. "
            "Единственная ручка калибровки VAD, крутится по месту"
        ),
    )
    vad_silence_s: float = Field(
        default=0.8,
        gt=0.0,
        description="Пауза, после которой реплика считается законченной",
    )
    vad_start_timeout_s: float = Field(
        default=3.0,
        gt=0.0,
        description=(
            "Сколько ждать начала речи после wake word. Истекло — ложное срабатывание, "
            "реплики не было"
        ),
    )
    vad_max_utterance_s: float = Field(
        default=30.0,
        gt=0.0,
        description="Потолок длины реплики: дольше — обрезаем и распознаём что есть",
    )
    log_level: str = "INFO"

    @field_validator("stt_language", mode="before")
    @classmethod
    def _blank_language_means_auto(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() or None

        return value

    @field_validator("wake_word_model_path", "input_device", mode="before")
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None

        return value


@lru_cache(maxsize=1)
def get_voice_settings() -> VoiceSettings:
    return VoiceSettings()
