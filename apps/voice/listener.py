from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import numpy as np

from apps.voice.audio import FrameQueue, Int16Frame
from apps.voice.state import VoiceStateMachine
from apps.voice.wake_word import WakeWordDetector
from libs.core.logging import get_logger

logger = get_logger(__name__)

UNSET_EPOCH: Final[int] = 0
POLL_TIMEOUT_S: Final[float] = 0.1


@dataclass(frozen=True, slots=True)
class WakeWordEvent:
    epoch: int
    phrase: str
    score: float
    detected_at: float


EpochProvider = Callable[[], int]
WakeWordSink = Callable[[WakeWordEvent], None]


def unset_epoch() -> int:
    return UNSET_EPOCH


class AsyncioWakeWordSink:
    def __init__(
        self, loop: asyncio.AbstractEventLoop, events: asyncio.Queue[WakeWordEvent]
    ) -> None:
        self._loop = loop
        self._events = events

    def __call__(self, event: WakeWordEvent) -> None:
        try:
            self._loop.call_soon_threadsafe(self._events.put_nowait, event)
        except RuntimeError:
            logger.debug("voice.wake_word_dropped_on_shutdown", epoch=event.epoch)


class WakeWordListener:
    def __init__(
        self,
        *,
        frames: FrameQueue,
        detector: WakeWordDetector,
        state: VoiceStateMachine,
        on_wake_word: WakeWordSink,
        threshold: float,
        epoch_provider: EpochProvider = unset_epoch,
        name: str = "voice-listener",
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1], got {threshold}")

        self._frames = frames
        self._detector = detector
        self._state = state
        self._on_wake_word = on_wake_word
        self._threshold = threshold
        self._epoch_provider = epoch_provider
        self._name = name
        self._buffer: Int16Frame = np.empty(0, dtype=np.int16)
        self._enabled = state.wake_word_enabled
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("listener is already running")

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is None:
            return

        self._thread.join(timeout)
        self._thread = None

    def feed(self, frame: Int16Frame) -> None:
        enabled = self._state.wake_word_enabled
        if enabled is not self._enabled:
            self._enabled = enabled
            self._reset()
        if not enabled:
            return

        self._buffer = np.concatenate((self._buffer, frame))
        frame_size = self._detector.frame_size
        while self._buffer.size >= frame_size:
            chunk = self._buffer[:frame_size]
            self._buffer = self._buffer[frame_size:]
            score = self._detector.score(chunk)
            if score >= self._threshold:
                self._trigger(score)
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self._frames.get(POLL_TIMEOUT_S)
            if frame is not None:
                self.feed(frame)

    def _trigger(self, score: float) -> None:
        if not self._state.try_begin_listening():
            self._reset()
            return

        epoch = self._epoch_provider()
        event = WakeWordEvent(
            epoch=epoch,
            phrase=self._detector.phrase,
            score=score,
            detected_at=time.monotonic(),
        )
        self._enabled = False
        self._reset()
        logger.info(
            "voice.wake_word_detected",
            phrase=event.phrase,
            score=round(score, 3),
            epoch=epoch,
        )
        self._on_wake_word(event)

    def _reset(self) -> None:
        self._buffer = np.empty(0, dtype=np.int16)
        self._detector.reset()
