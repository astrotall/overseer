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

    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state

    @property
    def wake_word_enabled(self) -> bool:
        return self.state is VoiceState.IDLE

    def set(self, state: VoiceState) -> None:
        with self._lock:
            self._state = state

    def try_begin_listening(self) -> bool:
        with self._lock:
            if self._state is not VoiceState.IDLE:
                return False

            self._state = VoiceState.LISTENING
            return True
