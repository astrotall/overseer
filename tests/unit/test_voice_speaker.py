from __future__ import annotations

import asyncio
import threading

import numpy as np

from apps.voice.audio import FrameQueue, Int16Frame, QueuedFrame
from apps.voice.listener import VoiceListener
from apps.voice.pipeline import VoiceSpeaker
from apps.voice.playback import SpeechPlayer
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.tts import Speech
from apps.voice.vad import Endpointer

SAMPLE_RATE = 24_000
BLOCK = 4
FRAME_SIZE = 4


class FakeDetector:
    def __init__(self) -> None:
        self.chunks: list[Int16Frame] = []
        self.resets = 0

    @property
    def phrase(self) -> str:
        return "hey jarvis"

    @property
    def frame_size(self) -> int:
        return FRAME_SIZE

    def score(self, frame: Int16Frame) -> float:
        self.chunks.append(frame)
        return 0.0

    def reset(self) -> None:
        self.resets += 1


class FakeTTS:
    def __init__(self, *, samples: int = 40, fails: bool = False) -> None:
        self._samples = samples
        self._fails = fails
        self.calls: list[str] = []

    @property
    def samplerate(self) -> int:
        return SAMPLE_RATE

    async def synthesize(self, text: str) -> Speech:
        self.calls.append(text)
        if self._fails:
            raise RuntimeError("модель синтеза не загрузилась")

        return Speech(samples=np.ones(self._samples, dtype=np.int16), samplerate=SAMPLE_RATE)


class HoldingSink:
    def __init__(self, *, fails: bool = False) -> None:
        self._fails = fails
        self.blocks: list[Int16Frame] = []
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.holds = False

    @property
    def samplerate(self) -> int:
        return SAMPLE_RATE

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def write(self, block: Int16Frame) -> None:
        if self._fails:
            raise OSError("устройство вывода исчезло")

        self.blocks.append(block)
        if self.holds:
            self.blocked.set()
            self.release.wait(2.0)


def make_listener(detector: FakeDetector, state: VoiceStateMachine) -> VoiceListener:
    return VoiceListener(
        frames=FrameQueue(),
        detector=detector,
        endpointer=Endpointer(
            speech_rms=1000.0,
            silence_s=0.05,
            start_timeout_s=0.05,
            max_duration_s=1.0,
        ),
        state=state,
        on_wake_word=lambda event: None,
        on_utterance=lambda utterance: None,
        threshold=0.5,
    )


def feed_now(listener: VoiceListener, state: VoiceStateMachine) -> None:
    listener.feed(
        QueuedFrame(generation=state.generation, samples=np.ones(FRAME_SIZE, dtype=np.int16))
    )


def make_speaker(
    state: VoiceStateMachine, tts: FakeTTS, sink: HoldingSink
) -> tuple[VoiceSpeaker, SpeechPlayer]:
    player = SpeechPlayer(
        sink=sink,
        generation_provider=lambda: state.generation,
        block_samples=BLOCK,
    )
    return VoiceSpeaker(tts=tts, player=player, state=state), player


async def test_wake_word_goes_deaf_while_the_agent_speaks_and_hears_again_after() -> None:
    state = VoiceStateMachine()
    detector = FakeDetector()
    listener = make_listener(detector, state)
    sink = HoldingSink()
    sink.holds = True
    speaker, player = make_speaker(state, FakeTTS(), sink)
    player.start()

    try:
        speaking = asyncio.create_task(speaker.speak("Отчёт за август готов."))
        await asyncio.to_thread(sink.blocked.wait, 2.0)

        assert state.state is VoiceState.SPEAKING
        assert not state.wake_word_enabled
        feed_now(listener, state)
        assert detector.chunks == []

        sink.release.set()
        assert await speaking is True
    finally:
        player.stop()

    assert state.state is VoiceState.IDLE
    assert state.wake_word_enabled
    feed_now(listener, state)
    assert len(detector.chunks) == 1


async def test_wake_word_comes_back_after_playback_fails() -> None:
    state = VoiceStateMachine()
    detector = FakeDetector()
    listener = make_listener(detector, state)
    speaker, player = make_speaker(state, FakeTTS(), HoldingSink(fails=True))
    player.start()

    try:
        assert await speaker.speak("Отчёт за август готов.") is False
    finally:
        player.stop()

    assert state.state is VoiceState.IDLE
    assert state.wake_word_enabled
    feed_now(listener, state)
    assert len(detector.chunks) == 1


async def test_wake_word_comes_back_after_synthesis_fails() -> None:
    state = VoiceStateMachine()
    detector = FakeDetector()
    listener = make_listener(detector, state)
    sink = HoldingSink()
    speaker, player = make_speaker(state, FakeTTS(fails=True), sink)
    player.start()

    try:
        assert await speaker.speak("Отчёт за август готов.") is False
    finally:
        player.stop()

    assert sink.blocks == []
    assert state.state is VoiceState.IDLE
    feed_now(listener, state)
    assert len(detector.chunks) == 1


async def test_wake_word_comes_back_when_the_player_was_never_started() -> None:
    state = VoiceStateMachine()
    tts = FakeTTS()
    speaker, _player = make_speaker(state, tts, HoldingSink())

    assert await speaker.speak("Отчёт за август готов.") is False
    assert tts.calls == ["Отчёт за август готов."]
    assert state.state is VoiceState.IDLE
    assert state.wake_word_enabled


async def test_text_without_letters_never_reaches_the_engine() -> None:
    state = VoiceStateMachine()
    tts = FakeTTS()
    sink = HoldingSink()
    speaker, player = make_speaker(state, tts, sink)
    player.start()

    try:
        assert await speaker.speak("  ...  ") is False
    finally:
        player.stop()

    assert tts.calls == []
    assert sink.blocks == []
    assert state.state is VoiceState.IDLE


async def test_two_speaks_take_turns_instead_of_overlapping() -> None:
    state = VoiceStateMachine()
    tts = FakeTTS(samples=8)
    sink = HoldingSink()
    speaker, player = make_speaker(state, tts, sink)
    player.start()

    try:
        results = await asyncio.gather(
            speaker.speak("Первый ответ."),
            speaker.speak("Второй ответ."),
        )
    finally:
        player.stop()

    assert results == [True, True]
    assert tts.calls == ["Первый ответ.", "Второй ответ."]
    assert len(sink.blocks) == 4
    assert state.state is VoiceState.IDLE


async def test_speaking_bumps_the_generation_so_stale_frames_are_dropped() -> None:
    state = VoiceStateMachine()
    detector = FakeDetector()
    listener = make_listener(detector, state)
    sink = HoldingSink()
    speaker, player = make_speaker(state, FakeTTS(samples=8), sink)
    player.start()
    stale = QueuedFrame(generation=state.generation, samples=np.ones(FRAME_SIZE, dtype=np.int16))

    try:
        assert await speaker.speak("Отчёт за август готов.") is True
    finally:
        player.stop()

    listener.feed(stale)

    assert detector.chunks == []
    assert detector.resets >= 1
