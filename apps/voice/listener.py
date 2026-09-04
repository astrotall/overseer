from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Generic, TypeVar

import numpy as np

from apps.voice.audio import FrameQueue, Int16Frame, QueuedFrame
from apps.voice.state import ConnectionGate, VoiceState, VoiceStateMachine
from apps.voice.vad import Endpoint, Endpointer, EndpointOutcome
from apps.voice.wake_word import WakeWordDetector
from libs.core.logging import get_logger

logger = get_logger(__name__)

UNSET_EPOCH: Final[int] = 0
POLL_TIMEOUT_S: Final[float] = 0.1

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WakeWordEvent:
    epoch: int
    phrase: str
    score: float
    detected_at: float


@dataclass(frozen=True, slots=True)
class Utterance:
    epoch: int
    outcome: EndpointOutcome
    samples: Int16Frame
    duration_s: float
    truncated: bool


EpochProvider = Callable[[], int]
WakeWordSink = Callable[[WakeWordEvent], None]
UtteranceSink = Callable[[Utterance], None]


def unset_epoch() -> int:
    return UNSET_EPOCH


class AsyncioSink(Generic[T]):
    def __init__(self, loop: asyncio.AbstractEventLoop, items: asyncio.Queue[T]) -> None:
        self._loop = loop
        self._items = items

    def __call__(self, item: T) -> None:
        try:
            self._loop.call_soon_threadsafe(self._items.put_nowait, item)
        except RuntimeError:
            logger.debug("voice.sink_dropped_on_shutdown", item=type(item).__name__)


class VoiceListener:
    def __init__(
        self,
        *,
        frames: FrameQueue,
        detector: WakeWordDetector,
        endpointer: Endpointer,
        state: VoiceStateMachine,
        on_wake_word: WakeWordSink,
        on_utterance: UtteranceSink,
        threshold: float,
        epoch_provider: EpochProvider = unset_epoch,
        gate: ConnectionGate | None = None,
        name: str = "voice-listener",
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1], got {threshold}")

        self._frames = frames
        self._detector = detector
        self._endpointer = endpointer
        self._state = state
        self._on_wake_word = on_wake_word
        self._on_utterance = on_utterance
        self._threshold = threshold
        self._epoch_provider = epoch_provider
        self._gate = gate if gate is not None else ConnectionGate(opened=True)
        self._name = name
        self._buffer: Int16Frame = np.empty(0, dtype=np.int16)
        self._generation = state.generation
        self._opened = self._gate.is_open
        self._recording_epoch: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lifecycle:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive():
                    raise RuntimeError(
                        "listener thread is still running: stop() has not joined it yet"
                    )
                self._thread = None

            self._stop.clear()
            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lifecycle:
            self._stop.set()
            thread = self._thread
            if thread is None:
                return

            thread.join(timeout)
            if thread.is_alive():
                logger.warning("voice.listener_stop_timed_out", thread=thread.name, timeout=timeout)
                return

            self._thread = None

    def feed(self, frame: QueuedFrame) -> None:
        state, generation = self._state.snapshot()
        if not self._gate_open(state):
            return
        if generation != self._generation:
            self._reset(generation)
        if frame.generation != generation:
            return

        if state is VoiceState.IDLE:
            self._detect(frame.samples, generation)
        elif state is VoiceState.LISTENING:
            self._record(frame.samples)

    def _run(self) -> None:
        while not self._stop.is_set():
            frame = self._frames.get(POLL_TIMEOUT_S)
            if frame is not None:
                self.feed(frame)

    def _gate_open(self, state: VoiceState) -> bool:
        is_open = self._gate.is_open
        if is_open == self._opened:
            return is_open

        self._opened = is_open
        if is_open:
            self._resume()
        else:
            self._suspend(state)
        return False

    def _resume(self) -> None:
        self._frames.clear()
        self._reset(self._state.generation)
        logger.info("voice.listener_resumed")

    def _suspend(self, state: VoiceState) -> None:
        logger.info("voice.listener_suspended", state=state.value)
        self._state.try_transition(VoiceState.LISTENING, VoiceState.IDLE)
        self._reset(self._state.generation)

    def _detect(self, samples: Int16Frame, generation: int) -> None:
        self._buffer = np.concatenate((self._buffer, samples))
        frame_size = self._detector.frame_size
        while self._buffer.size >= frame_size:
            chunk = self._buffer[:frame_size]
            self._buffer = self._buffer[frame_size:]
            score = self._detector.score(chunk)
            if score >= self._threshold:
                self._trigger(score, generation)
                return

    def _trigger(self, score: float, generation: int) -> None:
        if not self._state.try_begin_listening(generation=generation):
            self._reset(self._state.generation)
            return

        epoch = self._epoch_provider()
        event = WakeWordEvent(
            epoch=epoch,
            phrase=self._detector.phrase,
            score=score,
            detected_at=time.monotonic(),
        )
        self._reset(self._state.generation)
        self._recording_epoch = epoch
        logger.info(
            "voice.wake_word_detected",
            phrase=event.phrase,
            score=round(score, 3),
            epoch=epoch,
        )
        self._on_wake_word(event)

    def _record(self, samples: Int16Frame) -> None:
        if self._recording_epoch is None:
            return

        endpoint = self._endpointer.feed(samples)
        if endpoint is None:
            return

        epoch = self._recording_epoch
        self._recording_epoch = None
        current = self._epoch_provider()
        if epoch != current:
            logger.debug("voice.utterance_dropped_stale_epoch", epoch=epoch, current=current)
            self._state.set(VoiceState.IDLE)
            return

        self._state.set(VoiceState.THINKING)
        self._emit(epoch, endpoint)

    def _emit(self, epoch: int, endpoint: Endpoint) -> None:
        logger.info(
            "voice.utterance_captured",
            epoch=epoch,
            outcome=endpoint.outcome.value,
            duration_s=round(endpoint.duration_s, 2),
            truncated=endpoint.truncated,
        )
        self._on_utterance(
            Utterance(
                epoch=epoch,
                outcome=endpoint.outcome,
                samples=endpoint.samples,
                duration_s=endpoint.duration_s,
                truncated=endpoint.truncated,
            )
        )

    def _reset(self, generation: int) -> None:
        self._generation = generation
        self._buffer = np.empty(0, dtype=np.int16)
        self._recording_epoch = None
        self._detector.reset()
        self._endpointer.reset()
