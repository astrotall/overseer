from __future__ import annotations

import queue
from typing import Any, Final, TypeAlias

import numpy as np

SAMPLE_RATE: Final[int] = 16_000
CHANNELS: Final[int] = 1
FRAME_SAMPLES: Final[int] = 1280
FRAME_DURATION_S: Final[float] = FRAME_SAMPLES / SAMPLE_RATE
QUEUE_MAX_FRAMES: Final[int] = 25

Int16Frame: TypeAlias = np.ndarray[Any, np.dtype[np.int16]]


class FrameQueue:
    def __init__(self, maxsize: int = QUEUE_MAX_FRAMES) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize}")

        self._queue: queue.Queue[Int16Frame] = queue.Queue(maxsize=maxsize)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def put(self, frame: Int16Frame) -> None:
        while True:
            try:
                self._queue.put_nowait(frame)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    continue
                self._dropped += 1

    def get(self, timeout: float) -> Int16Frame | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
