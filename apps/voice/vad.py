from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from apps.voice.audio import SAMPLE_RATE, Int16Frame
from apps.voice.config import VoiceSettings


class EndpointOutcome(StrEnum):
    SPEECH = "speech"
    NO_SPEECH = "no_speech"


@dataclass(frozen=True, slots=True)
class Endpoint:
    outcome: EndpointOutcome
    samples: Int16Frame
    duration_s: float
    truncated: bool


def frame_rms(samples: Int16Frame) -> float:
    if samples.size == 0:
        return 0.0

    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


class Endpointer:
    def __init__(
        self,
        *,
        speech_rms: float,
        silence_s: float,
        start_timeout_s: float,
        max_duration_s: float,
    ) -> None:
        if speech_rms <= 0.0:
            raise ValueError(f"speech_rms must be positive, got {speech_rms}")
        if silence_s <= 0.0:
            raise ValueError(f"silence_s must be positive, got {silence_s}")
        if start_timeout_s <= 0.0:
            raise ValueError(f"start_timeout_s must be positive, got {start_timeout_s}")
        if max_duration_s <= start_timeout_s:
            raise ValueError(
                f"max_duration_s must exceed start_timeout_s, got {max_duration_s} "
                f"and {start_timeout_s}"
            )

        self._speech_rms = speech_rms
        self._silence_s = silence_s
        self._start_timeout_s = start_timeout_s
        self._max_duration_s = max_duration_s
        self._chunks: list[Int16Frame] = []
        self._elapsed_s = 0.0
        self._silence_run_s = 0.0
        self._speech_started = False

    @classmethod
    def from_settings(cls, settings: VoiceSettings) -> Endpointer:
        return cls(
            speech_rms=settings.vad_speech_rms,
            silence_s=settings.vad_silence_s,
            start_timeout_s=settings.vad_start_timeout_s,
            max_duration_s=settings.vad_max_utterance_s,
        )

    @property
    def speech_started(self) -> bool:
        return self._speech_started

    def is_speech(self, samples: Int16Frame) -> bool:
        return frame_rms(samples) >= self._speech_rms

    def reset(self) -> None:
        self._chunks = []
        self._elapsed_s = 0.0
        self._silence_run_s = 0.0
        self._speech_started = False

    def feed(self, samples: Int16Frame) -> Endpoint | None:
        if samples.size == 0:
            return None

        duration_s = samples.size / SAMPLE_RATE
        self._chunks.append(samples)
        self._elapsed_s += duration_s
        speech = self.is_speech(samples)

        if not self._speech_started:
            if speech:
                self._speech_started = True
                self._silence_run_s = 0.0
            elif self._elapsed_s >= self._start_timeout_s:
                return self._finish(EndpointOutcome.NO_SPEECH, truncated=False)
            return None

        if speech:
            self._silence_run_s = 0.0
        else:
            self._silence_run_s += duration_s
            if self._silence_run_s >= self._silence_s:
                return self._finish(EndpointOutcome.SPEECH, truncated=False)

        if self._elapsed_s >= self._max_duration_s:
            return self._finish(EndpointOutcome.SPEECH, truncated=True)

        return None

    def _finish(self, outcome: EndpointOutcome, *, truncated: bool) -> Endpoint:
        if outcome is EndpointOutcome.SPEECH and self._chunks:
            samples = np.concatenate(self._chunks)
        else:
            samples = np.empty(0, dtype=np.int16)

        endpoint = Endpoint(
            outcome=outcome,
            samples=samples,
            duration_s=self._elapsed_s,
            truncated=truncated,
        )
        self.reset()
        return endpoint
