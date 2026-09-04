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

    def try_transition(
        self, expected: VoiceState, target: VoiceState, *, generation: int | None = None
    ) -> bool:
        with self._lock:
            if self._state is not expected or target is expected:
                return False
            if generation is not None and generation != self._generation:
                return False

            self._state = target
            self._generation += 1
            return True

    def try_begin_listening(self, *, generation: int | None = None) -> bool:
        return self.try_transition(VoiceState.IDLE, VoiceState.LISTENING, generation=generation)

    def invalidate_input(self) -> bool:
        with self._lock:
            if self._state is VoiceState.SPEAKING:
                return False

            self._generation += 1
            return True


class ConnectionGate:
    def __init__(self, state: VoiceStateMachine | None = None, *, opened: bool = False) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._opened = threading.Event()
        if opened:
            self._opened.set()

    @property
    def is_open(self) -> bool:
        return self._opened.is_set()

    def open(self) -> None:
        self._switch(opened=True)

    def close(self) -> None:
        self._switch(opened=False)

    def _switch(self, *, opened: bool) -> None:
        with self._lock:
            if opened is self._opened.is_set():
                return

            if self._state is not None:
                self._state.invalidate_input()

            if opened:
                self._opened.set()
            else:
                self._opened.clear()
