from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np

from apps.voice.audio import Int16Frame
from apps.voice.config import VoiceSettings
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

logger = get_logger(__name__)

INT16_FULL_SCALE: Final[float] = 32768.0
BEAM_SIZE: Final[int] = 5
MIN_TRANSCRIPT_CHARS: Final[int] = 2
NO_SPEECH_PROB_LIMIT: Final[float] = 0.7
AVG_LOGPROB_LIMIT: Final[float] = -1.0

LETTER_OR_DIGIT: Final[re.Pattern[str]] = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Transcription:
    text: str
    language: str | None
    no_speech_prob: float
    avg_logprob: float


class SpeechToText(Protocol):
    async def transcribe(self, samples: Int16Frame) -> Transcription: ...


def to_float32(samples: Int16Frame) -> np.ndarray[Any, np.dtype[np.float32]]:
    return samples.astype(np.float32) / INT16_FULL_SCALE


def normalize_transcript(text: str) -> str:
    return " ".join(text.split())


def is_meaningful(transcription: Transcription) -> bool:
    text = normalize_transcript(transcription.text)
    if len(text) < MIN_TRANSCRIPT_CHARS:
        return False
    if LETTER_OR_DIGIT.search(text) is None:
        return False
    if transcription.no_speech_prob > NO_SPEECH_PROB_LIMIT:
        return False

    return transcription.avg_logprob >= AVG_LOGPROB_LIMIT


class FasterWhisperSTT:
    def __init__(
        self,
        *,
        model_size: str,
        device: str = "auto",
        compute_type: str = "int8",
        language: str | None = "ru",
    ) -> None:
        from faster_whisper import WhisperModel

        self._language = language
        try:
            self._model: WhisperModel = WhisperModel(
                model_size, device=device, compute_type=compute_type
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigurationError(
                f"Не удалось загрузить модель faster-whisper «{model_size}» "
                f"(device={device}, compute_type={compute_type}): {exc}"
            ) from exc

        logger.info(
            "voice.stt_model_loaded",
            model=model_size,
            device=device,
            compute_type=compute_type,
            language=language or "auto",
        )

    @classmethod
    def from_settings(cls, settings: VoiceSettings) -> FasterWhisperSTT:
        return cls(
            model_size=settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
            language=settings.stt_language,
        )

    async def transcribe(self, samples: Int16Frame) -> Transcription:
        return await asyncio.to_thread(self._transcribe, samples)

    def _transcribe(self, samples: Int16Frame) -> Transcription:
        segments, info = self._model.transcribe(
            to_float32(samples),
            language=self._language,
            beam_size=BEAM_SIZE,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        texts: list[str] = []
        no_speech_probs: list[float] = []
        avg_logprobs: list[float] = []
        for segment in segments:
            texts.append(segment.text)
            no_speech_probs.append(float(segment.no_speech_prob))
            avg_logprobs.append(float(segment.avg_logprob))

        return Transcription(
            text=normalize_transcript("".join(texts)),
            language=info.language,
            no_speech_prob=min(no_speech_probs, default=1.0),
            avg_logprob=max(avg_logprobs, default=0.0),
        )
