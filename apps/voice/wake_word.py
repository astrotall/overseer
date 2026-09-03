from __future__ import annotations

from pathlib import Path
from typing import Protocol

from apps.voice.audio import FRAME_SAMPLES, Int16Frame
from apps.voice.config import VoiceSettings
from libs.core.exceptions import ConfigurationError

INFERENCE_FRAMEWORK = "onnx"


class WakeWordDetector(Protocol):
    @property
    def phrase(self) -> str: ...

    @property
    def frame_size(self) -> int: ...

    def score(self, frame: Int16Frame) -> float: ...

    def reset(self) -> None: ...


def phrase_to_model_name(phrase: str) -> str:
    return "_".join(phrase.strip().lower().split())


class OpenWakeWordDetector:
    def __init__(self, *, phrase: str, model_path: Path | None = None) -> None:
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        self._phrase = phrase

        if model_path is not None:
            if not model_path.is_file():
                raise ConfigurationError(
                    f"Модель wake word не найдена: {model_path} (VOICE_WAKE_WORD_MODEL_PATH)"
                )
            model_name = model_path.stem
            model_ref = str(model_path)
        else:
            model_name = phrase_to_model_name(phrase)
            available = sorted(openwakeword.MODELS)
            if model_name not in available:
                raise ConfigurationError(
                    f"Для фразы «{phrase}» нет готовой модели openWakeWord. Обучи свою и "
                    "укажи VOICE_WAKE_WORD_MODEL_PATH либо возьми одну из готовых фраз: "
                    + ", ".join(name.replace("_", " ") for name in available)
                )
            model_ref = model_name

        download_models(model_names=[model_name])
        self._model = Model(wakeword_models=[model_ref], inference_framework=INFERENCE_FRAMEWORK)

    @classmethod
    def from_settings(cls, settings: VoiceSettings) -> OpenWakeWordDetector:
        return cls(phrase=settings.wake_word_phrase, model_path=settings.wake_word_model_path)

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def frame_size(self) -> int:
        return FRAME_SAMPLES

    def score(self, frame: Int16Frame) -> float:
        scores = self._model.predict(frame)
        return float(max(scores.values(), default=0.0))

    def reset(self) -> None:
        self._model.reset()
