from __future__ import annotations

import threading
from enum import StrEnum


class VoiceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoiceStateMachine:
    def __init__(self, initial: VoiceState = VoiceState.IDLE) -> None:
        self._lock = threading.Lock()
        self._state = initial
        self._generation = 0

    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def wake_word_enabled(self) -> bool:
        return self.snapshot()[0] is VoiceState.IDLE

    def snapshot(self) -> tuple[VoiceState, int]:
        with self._lock:
            return self._state, self._generation

    def set(self, state: VoiceState) -> None:
        with self._lock:
            if state is self._state:
                return

            self._state = state
            self._generation += 1

    def try_begin_listening(self) -> bool:
        with self._lock:
            if self._state is not VoiceState.IDLE:
                return False

            self._state = VoiceState.LISTENING
            self._generation += 1
            return True


class ConnectionGate:
    def __init__(self, *, opened: bool = False) -> None:
        self._opened = threading.Event()
        if opened:
            self._opened.set()

    @property
    def is_open(self) -> bool:
        return self._opened.is_set()

    def open(self) -> None:
        self._opened.set()

    def close(self) -> None:
        self._opened.clear()
