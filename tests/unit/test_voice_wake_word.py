from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from apps.voice.audio import FrameQueue, Int16Frame
from apps.voice.listener import (
    AsyncioWakeWordSink,
    EpochProvider,
    WakeWordEvent,
    WakeWordListener,
    unset_epoch,
)
from apps.voice.state import VoiceState, VoiceStateMachine

FRAME_SIZE = 4


class FakeDetector:
    def __init__(self, scores: list[float], phrase: str = "hey jarvis") -> None:
        self._scores = list(scores)
        self._phrase = phrase
        self.chunks: list[Int16Frame] = []
        self.resets = 0

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def frame_size(self) -> int:
        return FRAME_SIZE

    def score(self, frame: Int16Frame) -> float:
        self.chunks.append(frame)
        return self._scores.pop(0) if self._scores else 0.0

    def reset(self) -> None:
        self.resets += 1


def make_frame(size: int = FRAME_SIZE, value: int = 1) -> Int16Frame:
    return np.full(size, value, dtype=np.int16)


def make_listener(
    detector: FakeDetector,
    *,
    state: VoiceStateMachine | None = None,
    threshold: float = 0.5,
    epoch_provider: EpochProvider = unset_epoch,
) -> tuple[WakeWordListener, list[WakeWordEvent], VoiceStateMachine]:
    events: list[WakeWordEvent] = []
    state = state or VoiceStateMachine()
    listener = WakeWordListener(
        frames=FrameQueue(),
        detector=detector,
        state=state,
        on_wake_word=events.append,
        threshold=threshold,
        epoch_provider=epoch_provider,
    )
    return listener, events, state


def test_listener_hands_over_event_when_score_reaches_threshold() -> None:
    detector = FakeDetector([0.1, 0.9])
    listener, events, state = make_listener(detector)

    listener.feed(make_frame())
    assert events == []

    listener.feed(make_frame())

    assert len(events) == 1
    assert events[0].phrase == "hey jarvis"
    assert events[0].score == pytest.approx(0.9)
    assert state.state is VoiceState.LISTENING


def test_listener_stays_silent_below_threshold() -> None:
    detector = FakeDetector([0.49, 0.2])
    listener, events, state = make_listener(detector, threshold=0.5)

    listener.feed(make_frame())
    listener.feed(make_frame())

    assert events == []
    assert state.state is VoiceState.IDLE


def test_listener_reads_epoch_at_the_moment_of_the_trigger() -> None:
    epochs = iter([7, 8, 9])
    detector = FakeDetector([0.0, 0.9])
    listener, events, _ = make_listener(detector, epoch_provider=lambda: next(epochs))

    listener.feed(make_frame())
    listener.feed(make_frame())

    assert [event.epoch for event in events] == [7]
    assert next(epochs) == 8


def test_listener_event_keeps_its_epoch_when_the_connection_reconnects() -> None:
    epoch = 3

    def current_epoch() -> int:
        return epoch

    detector = FakeDetector([0.9])
    listener, events, _ = make_listener(detector, epoch_provider=current_epoch)

    listener.feed(make_frame())
    epoch = 4

    assert events[0].epoch == 3


def test_listener_drops_frames_while_the_agent_is_not_idle() -> None:
    detector = FakeDetector([0.9])
    state = VoiceStateMachine(VoiceState.SPEAKING)
    listener, events, _ = make_listener(detector, state=state)

    listener.feed(make_frame())

    assert events == []
    assert detector.chunks == []


def test_listener_resumes_after_the_state_returns_to_idle() -> None:
    detector = FakeDetector([0.9, 0.9])
    listener, events, state = make_listener(detector)

    listener.feed(make_frame())
    listener.feed(make_frame())
    assert len(events) == 1

    state.set(VoiceState.IDLE)
    listener.feed(make_frame())

    assert len(events) == 2


def test_listener_rechunks_frames_to_the_size_the_detector_wants() -> None:
    detector = FakeDetector([0.0, 0.0, 0.0])
    listener, _, _ = make_listener(detector)

    listener.feed(make_frame(size=3))
    assert detector.chunks == []

    listener.feed(make_frame(size=6))

    assert [chunk.size for chunk in detector.chunks] == [FRAME_SIZE, FRAME_SIZE]


def test_listener_clears_the_detector_after_a_trigger() -> None:
    detector = FakeDetector([0.9])
    listener, _, _ = make_listener(detector)

    listener.feed(make_frame())

    assert detector.resets == 1


def test_listener_thread_delivers_the_event_from_the_frame_queue() -> None:
    detector = FakeDetector([0.0, 0.9])
    frames = FrameQueue()
    delivered = threading.Event()
    events: list[WakeWordEvent] = []

    def sink(event: WakeWordEvent) -> None:
        events.append(event)
        delivered.set()

    listener = WakeWordListener(
        frames=frames,
        detector=detector,
        state=VoiceStateMachine(),
        on_wake_word=sink,
        threshold=0.5,
        epoch_provider=lambda: 42,
    )
    listener.start()
    try:
        frames.put(make_frame())
        frames.put(make_frame())
        assert delivered.wait(timeout=5.0)
    finally:
        listener.stop()

    assert [event.epoch for event in events] == [42]


async def test_asyncio_sink_moves_the_event_from_the_worker_thread_into_the_loop() -> None:
    events: asyncio.Queue[WakeWordEvent] = asyncio.Queue()
    sink = AsyncioWakeWordSink(asyncio.get_running_loop(), events)
    event = WakeWordEvent(epoch=5, phrase="hey jarvis", score=0.9, detected_at=1.0)

    thread = threading.Thread(target=sink, args=(event,))
    thread.start()
    thread.join(timeout=5.0)

    assert await asyncio.wait_for(events.get(), timeout=5.0) == event


def test_listener_rejects_a_threshold_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match="threshold"):
        WakeWordListener(
            frames=FrameQueue(),
            detector=FakeDetector([]),
            state=VoiceStateMachine(),
            on_wake_word=lambda event: None,
            threshold=1.5,
        )


def test_frame_queue_drops_the_oldest_frame_when_it_overflows() -> None:
    frames = FrameQueue(maxsize=2)

    for value in (1, 2, 3):
        frames.put(make_frame(value=value))

    first, second = frames.get(0.1), frames.get(0.1)

    assert frames.dropped == 1
    assert first is not None
    assert second is not None
    assert (int(first[0]), int(second[0])) == (2, 3)
    assert frames.get(0.01) is None


def test_state_machine_lets_only_one_trigger_start_listening() -> None:
    state = VoiceStateMachine()

    assert state.try_begin_listening() is True
    assert state.try_begin_listening() is False
    assert state.wake_word_enabled is False
