from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from apps.voice.audio import CHANNELS, GenerationProvider, Int16Frame
from apps.voice.tts import Speech
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

logger = get_logger(__name__)

BLOCK_SAMPLES: Final[int] = 1024
QUEUE_MAX_ITEMS: Final[int] = 2
POLL_TIMEOUT_S: Final[float] = 0.1


class PlaybackOutcome(StrEnum):
    PLAYED = "played"
    DROPPED = "dropped"
    INTERRUPTED = "interrupted"


class PlaybackError(RuntimeError):
    pass


class AudioSink(Protocol):
    @property
    def samplerate(self) -> int: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def write(self, block: Int16Frame) -> None: ...


Resolver = Callable[[PlaybackOutcome | BaseException], None]


@dataclass(frozen=True, slots=True)
class PlaybackItem:
    generation: int
    samples: Int16Frame
    resolve: Resolver = field(repr=False)


class SpeechPlayer:
    def __init__(
        self,
        *,
        sink: AudioSink,
        generation_provider: GenerationProvider,
        block_samples: int = BLOCK_SAMPLES,
        maxsize: int = QUEUE_MAX_ITEMS,
        name: str = "voice-playback",
    ) -> None:
        if block_samples <= 0:
            raise ValueError(f"block_samples must be positive, got {block_samples}")

        self._sink = sink
        self._generation_provider = generation_provider
        self._block_samples = block_samples
        self._name = name
        self._queue: queue.Queue[PlaybackItem] = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._pending: list[PlaybackItem] = []
        self._pending_lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lifecycle:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive():
                    raise RuntimeError(
                        "playback thread is still running: stop() has not joined it yet"
                    )
                self._thread = None

            self._sink.start()
            self._stop.clear()
            thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lifecycle:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    logger.warning(
                        "voice.playback_stop_timed_out", thread=thread.name, timeout=timeout
                    )
                    self._abandon(PlaybackOutcome.INTERRUPTED)
                    return

                self._thread = None

            self._sink.stop()
            self._abandon(PlaybackOutcome.INTERRUPTED)

    async def play(self, speech: Speech) -> PlaybackOutcome:
        if speech.samplerate != self._sink.samplerate:
            raise PlaybackError(
                f"частота синтеза {speech.samplerate} Hz не совпадает с частотой "
                f"вывода {self._sink.samplerate} Hz"
            )
        if speech.samples.size == 0:
            return PlaybackOutcome.PLAYED

        loop = asyncio.get_running_loop()
        finished: asyncio.Future[PlaybackOutcome] = loop.create_future()
        item = PlaybackItem(
            generation=self._generation_provider(),
            samples=speech.samples,
            resolve=_threadsafe_resolver(loop, finished),
        )

        with self._lifecycle:
            if self._thread is None or not self._thread.is_alive():
                raise PlaybackError("playback is not running: start() has not been called")

            with self._pending_lock:
                self._pending.append(item)

            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._forget(item)
                logger.warning("voice.playback_dropped_queue_full", generation=item.generation)
                return PlaybackOutcome.DROPPED

        return await finished

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=POLL_TIMEOUT_S)
            except queue.Empty:
                continue

            self._play_item(item)

    def _play_item(self, item: PlaybackItem) -> None:
        try:
            result: PlaybackOutcome | BaseException
            try:
                result = self._render(item)
            except Exception as exc:
                logger.exception("voice.playback_failed", generation=item.generation)
                result = exc

            item.resolve(result)
        finally:
            self._forget(item)

    def _render(self, item: PlaybackItem) -> PlaybackOutcome:
        current = self._generation_provider()
        if item.generation != current:
            logger.debug(
                "voice.playback_dropped_stale_generation",
                generation=item.generation,
                current=current,
            )
            return PlaybackOutcome.DROPPED

        return self._write(item)

    def _write(self, item: PlaybackItem) -> PlaybackOutcome:
        samples = item.samples
        for start in range(0, samples.size, self._block_samples):
            if self._stop.is_set():
                return PlaybackOutcome.INTERRUPTED
            if self._generation_provider() != item.generation:
                logger.debug("voice.playback_cut_stale_generation", generation=item.generation)
                return PlaybackOutcome.DROPPED

            self._sink.write(samples[start : start + self._block_samples])

        return PlaybackOutcome.PLAYED

    def _abandon(self, outcome: PlaybackOutcome) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        with self._pending_lock:
            outstanding = tuple(self._pending)
            self._pending.clear()

        for item in outstanding:
            item.resolve(outcome)

    def _forget(self, item: PlaybackItem) -> None:
        with self._pending_lock:
            for index, pending in enumerate(self._pending):
                if pending is item:
                    del self._pending[index]
                    return


def _threadsafe_resolver(
    loop: asyncio.AbstractEventLoop, future: asyncio.Future[PlaybackOutcome]
) -> Resolver:
    def resolve(result: PlaybackOutcome | BaseException) -> None:
        try:
            loop.call_soon_threadsafe(_settle, future, result)
        except RuntimeError:
            logger.debug("voice.playback_resolve_dropped_on_shutdown")

    return resolve


def _settle(
    future: asyncio.Future[PlaybackOutcome], result: PlaybackOutcome | BaseException
) -> None:
    if future.done():
        return

    if isinstance(result, BaseException):
        future.set_exception(result)
    else:
        future.set_result(result)


class SoundDeviceSink:
    def __init__(
        self,
        *,
        samplerate: int,
        device: str | int | None = None,
        blocksize: int = BLOCK_SAMPLES,
    ) -> None:
        import sounddevice as sd

        self._sd = sd
        self._samplerate = samplerate
        self._device = device
        self._blocksize = blocksize
        self._stream: sd.OutputStream | None = None

    @property
    def samplerate(self) -> int:
        return self._samplerate

    def start(self) -> None:
        if self._stream is not None:
            return

        stream = None
        try:
            stream = self._sd.OutputStream(
                samplerate=self._samplerate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
            )
            stream.start()
        except self._sd.PortAudioError as exc:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise ConfigurationError(
                f"Не удалось открыть вывод звука "
                f"({self._device or 'системный по умолчанию'}) в формате "
                f"{self._samplerate} Hz mono int16: {exc}"
            ) from exc

        self._stream = stream
        logger.info(
            "voice.playback_started",
            device=self._device,
            samplerate=self._samplerate,
            blocksize=self._blocksize,
        )

    def stop(self) -> None:
        if self._stream is None:
            return

        self._stream.stop()
        self._stream.close()
        self._stream = None
        logger.info("voice.playback_stopped", device=self._device)

    def write(self, block: Int16Frame) -> None:
        stream = self._stream
        if stream is None:
            raise PlaybackError("output stream is closed")

        stream.write(block.reshape(-1, CHANNELS))
