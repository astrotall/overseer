from __future__ import annotations

from types import TracebackType
from typing import Any

import sounddevice as sd

from apps.voice.audio import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE, FrameQueue, Int16Frame
from libs.core.exceptions import ConfigurationError
from libs.core.logging import get_logger

logger = get_logger(__name__)


def resolve_device(device: str | None) -> str | int | None:
    if device is None:
        return None

    return int(device) if device.isdigit() else device


class AudioCapture:
    def __init__(
        self,
        frames: FrameQueue,
        *,
        device: str | int | None = None,
        blocksize: int = FRAME_SAMPLES,
    ) -> None:
        self._frames = frames
        self._device = device
        self._blocksize = blocksize
        self._stream: sd.InputStream | None = None
        self._overflows = 0

    @property
    def overflows(self) -> int:
        return self._overflows

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("capture is already running")

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
                callback=self._callback,
            )
            stream.start()
        except sd.PortAudioError as exc:
            raise ConfigurationError(
                f"Не удалось открыть микрофон ({self._device or 'системный по умолчанию'}) "
                f"в формате {SAMPLE_RATE} Hz mono int16: {exc}"
            ) from exc

        self._stream = stream
        logger.info(
            "voice.capture_started",
            device=self._device,
            samplerate=SAMPLE_RATE,
            blocksize=self._blocksize,
        )

    def stop(self) -> None:
        if self._stream is None:
            return

        self._stream.stop()
        self._stream.close()
        self._stream = None
        logger.info(
            "voice.capture_stopped", overflows=self._overflows, dropped=self._frames.dropped
        )

    def __enter__(self) -> AudioCapture:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _callback(
        self, indata: Int16Frame, frames: int, time: Any, status: sd.CallbackFlags
    ) -> None:
        if status:
            self._overflows += 1

        self._frames.put(indata[:, 0].copy())
