from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Final, Protocol

import numpy as np

from apps.voice.audio import SAMPLE_RATE, Int16Frame
from libs.core.logging import get_logger

logger = get_logger(__name__)

CUE_AMPLITUDE: Final[float] = 0.25
CUE_TONE_S: Final[float] = 0.12
CUE_FADE_S: Final[float] = 0.01
NOT_UNDERSTOOD_TONES: Final[tuple[float, ...]] = (660.0, 440.0)


class CueKind(StrEnum):
    NOT_UNDERSTOOD = "not_understood"


CUE_TONES: Final[dict[CueKind, tuple[float, ...]]] = {
    CueKind.NOT_UNDERSTOOD: NOT_UNDERSTOOD_TONES,
}


class Cue(Protocol):
    async def play(self, kind: CueKind) -> None: ...


def render_cue(kind: CueKind, *, samplerate: int = SAMPLE_RATE) -> Int16Frame:
    tones = [render_tone(frequency, samplerate=samplerate) for frequency in CUE_TONES[kind]]
    return np.concatenate(tones).astype(np.int16)


def render_tone(frequency: float, *, samplerate: int = SAMPLE_RATE) -> Int16Frame:
    count = int(samplerate * CUE_TONE_S)
    time = np.arange(count, dtype=np.float64) / samplerate
    wave = np.sin(2.0 * np.pi * frequency * time) * CUE_AMPLITUDE
    fade = max(1, int(samplerate * CUE_FADE_S))
    envelope = np.ones(count)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (wave * envelope * 32767.0).astype(np.int16)


class LogCue:
    async def play(self, kind: CueKind) -> None:
        logger.info("voice.cue", kind=kind.value)


class BeepCue:
    def __init__(self, *, device: str | int | None = None) -> None:
        import sounddevice as sd

        self._sd = sd
        self._device = device
        self._tones = {kind: render_cue(kind) for kind in CueKind}

    async def play(self, kind: CueKind) -> None:
        logger.info("voice.cue", kind=kind.value)
        await asyncio.to_thread(self._play, kind)

    def _play(self, kind: CueKind) -> None:
        try:
            self._sd.play(self._tones[kind], samplerate=SAMPLE_RATE, device=self._device)
            self._sd.wait()
        except self._sd.PortAudioError as exc:
            logger.warning("voice.cue_failed", kind=kind.value, error=str(exc))
