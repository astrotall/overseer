from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np

from apps.voice.audio import Int16Frame
from apps.voice.config import VoiceSettings
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

if TYPE_CHECKING:
    import torch

logger = get_logger(__name__)

INT16_PEAK: Final[float] = 32767.0
MAX_CHUNK_CHARS: Final[int] = 900
SENTENCE_BREAK: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?…])\s+")
CLAUSE_BREAK: Final[re.Pattern[str]] = re.compile(r"(?<=[,;:])\s+")
LETTER_OR_DIGIT: Final[re.Pattern[str]] = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Speech:
    samples: Int16Frame
    samplerate: int

    @property
    def duration_s(self) -> float:
        return self.samples.size / self.samplerate


class TextToSpeech(Protocol):
    @property
    def samplerate(self) -> int: ...

    async def synthesize(self, text: str) -> Speech: ...


def is_speakable(text: str) -> bool:
    return LETTER_OR_DIGIT.search(text) is not None


def split_for_synthesis(text: str, *, limit: int = MAX_CHUNK_CHARS) -> tuple[str, ...]:
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")

    normalized = " ".join(text.split())
    if not normalized:
        return ()

    chunks: list[str] = []
    for sentence in _split_by(normalized, SENTENCE_BREAK, limit):
        _append_chunk(chunks, sentence, limit)

    return tuple(chunks)


def _split_by(text: str, pattern: re.Pattern[str], limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    return [part for part in pattern.split(text) if part]


def _append_chunk(chunks: list[str], part: str, limit: int) -> None:
    if chunks and len(chunks[-1]) + 1 + len(part) <= limit:
        chunks[-1] = f"{chunks[-1]} {part}"
        return

    if len(part) <= limit:
        chunks.append(part)
        return

    clauses = _split_by(part, CLAUSE_BREAK, limit)
    if len(clauses) > 1:
        for clause in clauses:
            _append_chunk(chunks, clause, limit)
        return

    chunks.extend(_split_words(part, limit))


def _split_words(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    for word in text.split(" "):
        if chunks and len(chunks[-1]) + 1 + len(word) <= limit:
            chunks[-1] = f"{chunks[-1]} {word}"
        elif len(word) <= limit:
            chunks.append(word)
        else:
            chunks.extend(word[start : start + limit] for start in range(0, len(word), limit))

    return chunks


def to_int16(waveform: np.ndarray[Any, np.dtype[np.float32]]) -> Int16Frame:
    clipped = np.clip(waveform, -1.0, 1.0)
    return (clipped * INT16_PEAK).astype(np.int16)


class SileroTTS:
    def __init__(
        self,
        *,
        model_url: str,
        model_path: Path,
        voice: str,
        samplerate: int,
        device: str = "cpu",
        max_chunk_chars: int = MAX_CHUNK_CHARS,
    ) -> None:
        import torch

        self._torch = torch
        self._voice = voice
        self._samplerate = samplerate
        self._max_chunk_chars = max_chunk_chars

        weights = self._ensure_weights(model_url, model_path)
        try:
            self._model = torch.package.PackageImporter(str(weights)).load_pickle(
                "tts_models", "model"
            )
            self._model.to(torch.device(device))
        except Exception as exc:
            raise ConfigurationError(
                f"Не удалось загрузить модель Silero TTS из {weights} (device={device}): {exc}"
            ) from exc

        speakers = tuple(getattr(self._model, "speakers", ()))
        if speakers and voice not in speakers:
            raise ConfigurationError(
                f"Голос «{voice}» не найден в модели {weights.name} (VOICE_TTS_VOICE). "
                "Доступны: " + ", ".join(speakers)
            )

        logger.info(
            "voice.tts_model_loaded",
            model=weights.name,
            voice=voice,
            samplerate=samplerate,
            device=device,
        )

    @classmethod
    def from_settings(cls, settings: VoiceSettings) -> SileroTTS:
        return cls(
            model_url=settings.tts_model_url,
            model_path=settings.tts_model_file,
            voice=settings.tts_voice,
            samplerate=settings.tts_sample_rate,
            device=settings.tts_device,
        )

    @property
    def samplerate(self) -> int:
        return self._samplerate

    async def synthesize(self, text: str) -> Speech:
        return await asyncio.to_thread(self._synthesize, text)

    def _ensure_weights(self, model_url: str, model_path: Path) -> Path:
        if model_path.is_file():
            return model_path

        model_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("voice.tts_model_download", url=model_url, path=str(model_path))
        try:
            self._torch.hub.download_url_to_file(model_url, str(model_path), progress=False)
        except Exception as exc:
            raise ConfigurationError(
                f"Не удалось скачать модель Silero TTS с {model_url}: {exc}. Положи файл "
                f"в {model_path} вручную или задай VOICE_TTS_MODEL_PATH"
            ) from exc

        return model_path

    def _synthesize(self, text: str) -> Speech:
        chunks = split_for_synthesis(text, limit=self._max_chunk_chars)
        speakable = [chunk for chunk in chunks if is_speakable(chunk)]
        if not speakable:
            return Speech(samples=np.empty(0, dtype=np.int16), samplerate=self._samplerate)

        waves = [self._apply_tts(chunk) for chunk in speakable]
        return Speech(samples=np.concatenate(waves), samplerate=self._samplerate)

    def _apply_tts(self, chunk: str) -> Int16Frame:
        with self._torch.inference_mode():
            audio: torch.Tensor = self._model.apply_tts(
                text=chunk,
                speaker=self._voice,
                sample_rate=self._samplerate,
            )

        return to_int16(audio.detach().cpu().numpy().astype(np.float32))
