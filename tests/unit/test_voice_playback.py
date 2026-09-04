from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from apps.voice.audio import Int16Frame
from apps.voice.playback import (
    PlaybackError,
    PlaybackOutcome,
    SpeechPlayer,
)
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.tts import Speech

SAMPLE_RATE = 24_000
BLOCK = 4


class FakeSink:
    def __init__(self, *, samplerate: int = SAMPLE_RATE, fails_after: int | None = None) -> None:
        self._samplerate = samplerate
        self.fails_after = fails_after
        self.blocks: list[Int16Frame] = []
        self.started = 0
        self.stopped = 0
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.hold_from: int | None = None

    @property
    def samplerate(self) -> int:
        return self._samplerate

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def write(self, block: Int16Frame) -> None:
        if self.fails_after is not None and len(self.blocks) >= self.fails_after:
            raise OSError("устройство вывода исчезло")

        self.blocks.append(block)
        if self.hold_from is not None and len(self.blocks) >= self.hold_from:
            self.blocked.set()
            self.release.wait(2.0)

    @property
    def written(self) -> Int16Frame:
        if not self.blocks:
            return np.empty(0, dtype=np.int16)

        return np.concatenate(self.blocks)


class SteppingGeneration:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.calls


def speech(size: int = 10, *, samplerate: int = SAMPLE_RATE) -> Speech:
    return Speech(samples=np.arange(size, dtype=np.int16), samplerate=samplerate)


def make_player(sink: FakeSink, state: VoiceStateMachine) -> SpeechPlayer:
    return SpeechPlayer(
        sink=sink,
        generation_provider=lambda: state.generation,
        block_samples=BLOCK,
    )


async def test_a_whole_speech_reaches_the_sink_block_by_block() -> None:
    sink = FakeSink()
    state = VoiceStateMachine()
    player = make_player(sink, state)
    player.start()

    try:
        outcome = await player.play(speech(10))
    finally:
        player.stop()

    assert outcome is PlaybackOutcome.PLAYED
    assert [block.size for block in sink.blocks] == [4, 4, 2]
    assert sink.written.tolist() == list(range(10))


async def test_starting_the_player_opens_the_sink_and_stopping_closes_it() -> None:
    sink = FakeSink()
    player = make_player(sink, VoiceStateMachine())

    player.start()
    assert player.running
    assert (sink.started, sink.stopped) == (1, 0)

    player.stop()
    assert not player.running
    assert (sink.started, sink.stopped) == (1, 1)


async def test_a_second_start_on_a_live_thread_is_refused() -> None:
    sink = FakeSink()
    player = make_player(sink, VoiceStateMachine())
    player.start()

    try:
        with pytest.raises(RuntimeError, match="playback thread is still running"):
            player.start()
    finally:
        player.stop()

    assert sink.started == 1


async def test_playing_without_start_is_refused_instead_of_hanging() -> None:
    player = make_player(FakeSink(), VoiceStateMachine())

    with pytest.raises(PlaybackError, match="playback is not running"):
        await player.play(speech())


async def test_a_samplerate_that_does_not_match_the_sink_is_refused() -> None:
    sink = FakeSink(samplerate=24_000)
    player = make_player(sink, VoiceStateMachine())
    player.start()

    try:
        with pytest.raises(PlaybackError, match="не совпадает"):
            await player.play(speech(samplerate=48_000))
    finally:
        player.stop()

    assert sink.blocks == []


async def test_empty_speech_never_reaches_the_sink() -> None:
    sink = FakeSink()
    player = make_player(sink, VoiceStateMachine())
    player.start()

    try:
        outcome = await player.play(Speech(np.empty(0, dtype=np.int16), SAMPLE_RATE))
    finally:
        player.stop()

    assert outcome is PlaybackOutcome.PLAYED
    assert sink.blocks == []


async def test_speech_stamped_with_a_stale_generation_is_dropped_before_the_sink() -> None:
    sink = FakeSink()
    player = SpeechPlayer(
        sink=sink,
        generation_provider=SteppingGeneration(),
        block_samples=BLOCK,
    )
    player.start()

    try:
        outcome = await player.play(speech(10))
    finally:
        player.stop()

    assert outcome is PlaybackOutcome.DROPPED
    assert sink.blocks == []


async def test_a_state_change_mid_speech_cuts_the_rest_of_the_playback() -> None:
    sink = FakeSink()
    sink.hold_from = 1
    state = VoiceStateMachine()
    state.set(VoiceState.SPEAKING)
    player = make_player(sink, state)
    player.start()

    try:
        playing = asyncio.create_task(player.play(speech(40)))
        await asyncio.to_thread(sink.blocked.wait, 2.0)
        state.set(VoiceState.IDLE)
        sink.release.set()
        outcome = await playing
    finally:
        player.stop()

    assert outcome is PlaybackOutcome.DROPPED
    assert len(sink.blocks) < 10


async def test_a_sink_failure_reaches_the_caller_instead_of_killing_the_thread() -> None:
    sink = FakeSink(fails_after=1)
    state = VoiceStateMachine()
    player = make_player(sink, state)
    player.start()

    try:
        with pytest.raises(OSError, match="устройство вывода исчезло"):
            await player.play(speech(10))

        assert player.running
        sink.fails_after = None
        assert await player.play(speech(4)) is PlaybackOutcome.PLAYED
    finally:
        player.stop()


async def test_stopping_mid_speech_resolves_the_waiting_caller() -> None:
    sink = FakeSink()
    sink.hold_from = 1
    state = VoiceStateMachine()
    player = make_player(sink, state)
    player.start()

    playing = asyncio.create_task(player.play(speech(40)))
    await asyncio.to_thread(sink.blocked.wait, 2.0)
    stopping = asyncio.create_task(asyncio.to_thread(player.stop))
    await asyncio.sleep(0.05)
    sink.release.set()
    await stopping

    assert await asyncio.wait_for(playing, timeout=2.0) is PlaybackOutcome.INTERRUPTED
    assert sink.stopped == 1
    assert len(sink.blocks) < 10


async def test_a_non_positive_block_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="block_samples must be positive"):
        SpeechPlayer(sink=FakeSink(), generation_provider=lambda: 0, block_samples=0)
