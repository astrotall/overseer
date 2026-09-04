from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import numpy as np
import pytest

from apps.voice.audio import FrameQueue, Int16Frame, QueuedFrame
from apps.voice.listener import (
    AsyncioSink,
    EpochProvider,
    Utterance,
    VoiceListener,
    WakeWordEvent,
    unset_epoch,
)
from apps.voice.state import ConnectionGate, VoiceState, VoiceStateMachine
from apps.voice.vad import Endpointer, EndpointOutcome

FRAME_SIZE = 4
SPEECH_RMS = 4000
SILENCE_RMS = 10


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


class RacingStateMachine(VoiceStateMachine):
    def __init__(self, *, seen: VoiceState, switch_to: VoiceState) -> None:
        super().__init__()
        self._seen = seen
        self._switch_to = switch_to
        self.raced = False

    def snapshot(self) -> tuple[VoiceState, int]:
        observed = super().snapshot()
        if not self.raced and observed[0] is self._seen:
            self.raced = True
            super().set(self._switch_to)
        return observed


class ClearingFrameQueue(FrameQueue):
    def __init__(self, on_cleared: Callable[[], None]) -> None:
        super().__init__()
        self._on_cleared = on_cleared

    def clear(self) -> None:
        super().clear()
        self._on_cleared()


def make_frame(size: int = FRAME_SIZE, value: int = 1) -> Int16Frame:
    return np.full(size, value, dtype=np.int16)


def queued(generation: int, size: int = FRAME_SIZE, value: int = 1) -> QueuedFrame:
    return QueuedFrame(generation=generation, samples=make_frame(size, value))


def feed_now(
    listener: VoiceListener,
    state: VoiceStateMachine,
    size: int = FRAME_SIZE,
    value: int = 1,
) -> None:
    listener.feed(queued(state.generation, size=size, value=value))


def make_endpointer(
    *,
    silence_s: float = 0.001,
    start_timeout_s: float = 0.0005,
    max_duration_s: float = 1.0,
) -> Endpointer:
    return Endpointer(
        speech_rms=1000.0,
        silence_s=silence_s,
        start_timeout_s=start_timeout_s,
        max_duration_s=max_duration_s,
    )


def threads_named(name: str) -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == name)


def make_listener(
    detector: FakeDetector,
    *,
    state: VoiceStateMachine | None = None,
    threshold: float = 0.5,
    epoch_provider: EpochProvider = unset_epoch,
    endpointer: Endpointer | None = None,
    utterances: list[Utterance] | None = None,
    gate: ConnectionGate | None = None,
) -> tuple[VoiceListener, list[WakeWordEvent], VoiceStateMachine]:
    events: list[WakeWordEvent] = []
    state = state or VoiceStateMachine()
    listener = VoiceListener(
        frames=FrameQueue(),
        detector=detector,
        state=state,
        endpointer=endpointer or make_endpointer(),
        on_wake_word=events.append,
        on_utterance=(utterances if utterances is not None else []).append,
        threshold=threshold,
        epoch_provider=epoch_provider,
        gate=gate,
    )
    return listener, events, state


def test_listener_hands_over_event_when_score_reaches_threshold() -> None:
    detector = FakeDetector([0.1, 0.9])
    listener, events, state = make_listener(detector)

    feed_now(listener, state)
    assert events == []

    feed_now(listener, state)

    assert len(events) == 1
    assert events[0].phrase == "hey jarvis"
    assert events[0].score == pytest.approx(0.9)
    assert state.state is VoiceState.LISTENING


def test_listener_stays_silent_below_threshold() -> None:
    detector = FakeDetector([0.49, 0.2])
    listener, events, state = make_listener(detector, threshold=0.5)

    feed_now(listener, state)
    feed_now(listener, state)

    assert events == []
    assert state.state is VoiceState.IDLE


def test_listener_reads_epoch_at_the_moment_of_the_trigger() -> None:
    epochs = iter([7, 8, 9])
    detector = FakeDetector([0.0, 0.9])
    listener, events, state = make_listener(detector, epoch_provider=lambda: next(epochs))

    feed_now(listener, state)
    feed_now(listener, state)

    assert [event.epoch for event in events] == [7]
    assert next(epochs) == 8


def test_listener_event_keeps_its_epoch_when_the_connection_reconnects() -> None:
    epoch = 3

    def current_epoch() -> int:
        return epoch

    detector = FakeDetector([0.9])
    listener, events, state = make_listener(detector, epoch_provider=current_epoch)

    feed_now(listener, state)
    epoch = 4

    assert events[0].epoch == 3


def test_listener_drops_frames_while_the_agent_is_not_idle() -> None:
    detector = FakeDetector([0.9])
    state = VoiceStateMachine(VoiceState.SPEAKING)
    listener, events, _ = make_listener(detector, state=state)

    feed_now(listener, state)

    assert events == []
    assert detector.chunks == []


def test_listener_resumes_after_the_state_returns_to_idle() -> None:
    detector = FakeDetector([0.9, 0.9])
    listener, events, state = make_listener(detector)

    feed_now(listener, state)
    assert len(events) == 1

    state.set(VoiceState.IDLE)
    feed_now(listener, state)

    assert len(events) == 2


def test_listener_drops_frames_captured_before_the_state_changed() -> None:
    detector = FakeDetector([0.9, 0.9])
    listener, events, state = make_listener(detector)
    stale = queued(state.generation)

    state.set(VoiceState.THINKING)
    state.set(VoiceState.SPEAKING)
    state.set(VoiceState.IDLE)
    listener.feed(stale)

    assert detector.chunks == []
    assert events == []
    assert state.state is VoiceState.IDLE

    feed_now(listener, state)

    assert len(events) == 1


def test_listener_drops_frames_captured_before_the_wake_word_was_accepted() -> None:
    detector = FakeDetector([0.9, 0.9])
    listener, events, state = make_listener(detector)
    stale = queued(state.generation)

    feed_now(listener, state)
    assert len(events) == 1

    state.set(VoiceState.IDLE)
    listener.feed(stale)

    assert len(events) == 1
    assert len(detector.chunks) == 1


def test_listener_thread_drops_the_frames_the_queue_kept_from_the_previous_cycle() -> None:
    detector = FakeDetector([0.9])
    frames = FrameQueue()
    state = VoiceStateMachine()
    delivered = threading.Event()
    events: list[WakeWordEvent] = []

    def sink(event: WakeWordEvent) -> None:
        events.append(event)
        delivered.set()

    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=sink,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        name="voice-listener-stale",
    )
    stale = [queued(state.generation, value=value) for value in (1, 2, 3)]

    state.set(VoiceState.SPEAKING)
    state.set(VoiceState.IDLE)
    listener.start()
    try:
        for frame in stale:
            frames.put(frame.samples, frame.generation)
        assert not delivered.wait(timeout=0.5)
    finally:
        listener.stop()

    assert events == []
    assert detector.chunks == []


def test_listener_ignores_the_wake_word_while_the_connection_is_down() -> None:
    detector = FakeDetector([0.9])
    listener, events, state = make_listener(detector, gate=ConnectionGate())

    feed_now(listener, state)
    feed_now(listener, state)

    assert events == []
    assert detector.chunks == []
    assert state.state is VoiceState.IDLE


def test_listener_hears_the_wake_word_again_once_the_connection_is_back() -> None:
    gate = ConnectionGate()
    detector = FakeDetector([0.9])
    listener, events, state = make_listener(detector, gate=gate)

    feed_now(listener, state)
    assert events == []

    gate.open()
    feed_now(listener, state)
    feed_now(listener, state)

    assert len(events) == 1
    assert state.state is VoiceState.LISTENING


def test_listener_abandons_the_recording_when_the_connection_drops() -> None:
    gate = ConnectionGate(opened=True)
    detector = FakeDetector([0.9])
    utterances: list[Utterance] = []
    listener, events, state = make_listener(detector, gate=gate, utterances=utterances)

    feed_now(listener, state)
    assert len(events) == 1
    assert state.state is VoiceState.LISTENING

    gate.close()
    feed_now(listener, state)

    assert state.state is VoiceState.IDLE
    assert utterances == []


def test_a_dropped_connection_does_not_interrupt_the_answer_being_spoken() -> None:
    state = VoiceStateMachine(VoiceState.SPEAKING)
    gate = ConnectionGate(state, opened=True)
    listener, _, _ = make_listener(FakeDetector([0.9]), gate=gate, state=state)
    generation = state.generation

    gate.close()
    feed_now(listener, state)

    assert state.state is VoiceState.SPEAKING
    assert state.generation == generation


def test_listener_throws_away_the_frames_recorded_while_the_connection_was_down() -> None:
    state = VoiceStateMachine()
    gate = ConnectionGate(state, opened=True)
    frames = FrameQueue()
    detector = FakeDetector([0.9])
    events: list[WakeWordEvent] = []
    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=events.append,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        gate=gate,
    )

    gate.close()
    feed_now(listener, state)
    frames.put(make_frame(), state.generation)
    gate.open()
    feed_now(listener, state)

    assert frames.get(0.0) is None
    assert detector.chunks == []
    assert events == []


def test_a_drop_does_not_undo_a_state_that_changed_under_the_listener() -> None:
    state = RacingStateMachine(seen=VoiceState.LISTENING, switch_to=VoiceState.SPEAKING)
    gate = ConnectionGate(state, opened=True)
    utterances: list[Utterance] = []
    listener, events, _ = make_listener(
        FakeDetector([0.9]), gate=gate, state=state, utterances=utterances
    )

    feed_now(listener, state)
    assert len(events) == 1
    assert state.state is VoiceState.LISTENING

    gate.close()
    listener.feed(queued(state.generation))

    assert state.raced is True
    assert state.state is VoiceState.SPEAKING
    assert utterances == []


def test_a_frame_captured_while_the_connection_was_down_loses_the_race_with_the_clear() -> None:
    state = VoiceStateMachine()
    gate = ConnectionGate(state, opened=True)
    detector = FakeDetector([0.9])
    events: list[WakeWordEvent] = []
    captured_generation = 0

    def put_after_clear() -> None:
        thread = threading.Thread(
            target=frames.put, args=(make_frame(), captured_generation), name="portaudio"
        )
        thread.start()
        thread.join(1.0)
        assert not thread.is_alive()

    frames = ClearingFrameQueue(put_after_clear)
    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=events.append,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        gate=gate,
    )

    gate.close()
    listener.feed(queued(state.generation))
    captured_generation = state.generation
    gate.open()
    listener.feed(queued(state.generation))

    late = frames.get(0.0)
    assert late is not None
    assert late.generation == captured_generation

    listener.feed(late)

    assert detector.chunks == []
    assert events == []


def test_the_reconnect_drains_the_queue_even_when_a_stale_frame_arrives_first() -> None:
    state = VoiceStateMachine()
    gate = ConnectionGate(state, opened=True)
    frames = FrameQueue()
    detector = FakeDetector([0.9])
    events: list[WakeWordEvent] = []
    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=events.append,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        gate=gate,
    )

    gate.close()
    listener.feed(queued(state.generation))
    stale = queued(state.generation)
    frames.put(make_frame(value=2), stale.generation)
    frames.put(make_frame(value=3), stale.generation)
    gate.open()
    listener.feed(stale)

    assert frames.get(0.0) is None
    assert detector.chunks == []
    assert events == []


def test_an_echo_frame_outlives_a_gate_cycle_inside_speaking_and_dies_on_the_way_to_idle() -> None:
    state = VoiceStateMachine(VoiceState.SPEAKING)
    gate = ConnectionGate(state, opened=True)
    detector = FakeDetector([0.9])
    events: list[WakeWordEvent] = []
    listener = VoiceListener(
        frames=FrameQueue(),
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=events.append,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        gate=gate,
    )
    generation = state.generation

    gate.close()
    listener.feed(queued(generation))
    echo = queued(generation)
    gate.open()
    listener.feed(queued(generation))

    assert state.generation == generation

    listener.feed(echo)
    assert detector.chunks == []

    state.set(VoiceState.IDLE)
    assert state.generation == generation + 1

    listener.feed(echo)

    assert detector.chunks == []
    assert events == []


def test_a_frame_queued_while_speaking_with_the_gate_shut_is_gone_after_the_reconnect() -> None:
    state = VoiceStateMachine(VoiceState.SPEAKING)
    gate = ConnectionGate(state, opened=True)
    frames = FrameQueue()
    detector = FakeDetector([0.9])
    events: list[WakeWordEvent] = []
    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=events.append,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        gate=gate,
    )
    generation = state.generation

    gate.close()
    assert state.generation == generation

    echo = queued(generation, value=7)
    frames.put(echo.samples, echo.generation)
    listener.feed(queued(generation))
    assert state.state is VoiceState.SPEAKING

    state.set(VoiceState.IDLE)
    assert state.generation == generation + 1

    gate.open()
    assert state.generation == generation + 2

    listener.feed(queued(state.generation))

    assert frames.get(0.0) is None

    listener.feed(echo)

    assert detector.chunks == []
    assert events == []


def test_listener_rechunks_frames_to_the_size_the_detector_wants() -> None:
    detector = FakeDetector([0.0, 0.0, 0.0])
    listener, _, state = make_listener(detector)

    feed_now(listener, state, size=3)
    assert detector.chunks == []

    feed_now(listener, state, size=6)

    assert [chunk.size for chunk in detector.chunks] == [FRAME_SIZE, FRAME_SIZE]


def test_listener_clears_the_detector_after_a_trigger() -> None:
    detector = FakeDetector([0.9])
    listener, _, state = make_listener(detector)

    feed_now(listener, state)

    assert detector.resets == 1


def test_listener_thread_delivers_the_event_from_the_frame_queue() -> None:
    detector = FakeDetector([0.0, 0.9])
    frames = FrameQueue()
    state = VoiceStateMachine()
    delivered = threading.Event()
    events: list[WakeWordEvent] = []

    def sink(event: WakeWordEvent) -> None:
        events.append(event)
        delivered.set()

    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=sink,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        epoch_provider=lambda: 42,
    )
    listener.start()
    try:
        frames.put(make_frame(), state.generation)
        frames.put(make_frame(), state.generation)
        assert delivered.wait(timeout=5.0)
    finally:
        listener.stop()

    assert [event.epoch for event in events] == [42]


def test_listener_refuses_to_start_while_the_previous_thread_is_still_running() -> None:
    detector = FakeDetector([0.9])
    frames = FrameQueue()
    state = VoiceStateMachine()
    entered = threading.Event()
    release = threading.Event()
    name = "voice-listener-blocked"

    def blocking_sink(event: WakeWordEvent) -> None:
        entered.set()
        release.wait(timeout=5.0)

    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=blocking_sink,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        name=name,
    )
    listener.start()
    try:
        frames.put(make_frame(), state.generation)
        assert entered.wait(timeout=5.0)

        listener.stop(timeout=0.1)
        assert listener.running

        with pytest.raises(RuntimeError, match="still running"):
            listener.start()

        assert threads_named(name) == 1
    finally:
        release.set()
        listener.stop(timeout=5.0)

    assert not listener.running
    assert threads_named(name) == 0


def test_listener_starts_again_once_the_stuck_thread_has_finished() -> None:
    detector = FakeDetector([0.9])
    frames = FrameQueue()
    state = VoiceStateMachine()
    entered = threading.Event()
    release = threading.Event()
    name = "voice-listener-restart"

    def blocking_sink(event: WakeWordEvent) -> None:
        entered.set()
        release.wait(timeout=5.0)

    listener = VoiceListener(
        frames=frames,
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=blocking_sink,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        name=name,
    )
    listener.start()
    try:
        frames.put(make_frame(), state.generation)
        assert entered.wait(timeout=5.0)
        listener.stop(timeout=0.1)

        release.set()
        deadline = time.monotonic() + 5.0
        while listener.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not listener.running

        listener.start()

        assert threads_named(name) == 1
    finally:
        release.set()
        listener.stop(timeout=5.0)


def test_listener_starts_exactly_one_thread_when_two_callers_race() -> None:
    detector = FakeDetector([])
    state = VoiceStateMachine()
    name = "voice-listener-race"
    listener = VoiceListener(
        frames=FrameQueue(),
        detector=detector,
        state=state,
        endpointer=make_endpointer(),
        on_wake_word=lambda event: None,
        on_utterance=lambda utterance: None,
        threshold=0.5,
        name=name,
    )
    ready = threading.Barrier(2)
    refusals: list[RuntimeError] = []

    def racing_start() -> None:
        ready.wait(timeout=5.0)
        try:
            listener.start()
        except RuntimeError as exc:
            refusals.append(exc)

    racers = [threading.Thread(target=racing_start) for _ in range(2)]
    try:
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(timeout=5.0)

        assert threads_named(name) == 1
        assert len(refusals) == 1
    finally:
        listener.stop(timeout=5.0)

    assert threads_named(name) == 0


async def test_asyncio_sink_moves_the_event_from_the_worker_thread_into_the_loop() -> None:
    events: asyncio.Queue[WakeWordEvent] = asyncio.Queue()
    sink = AsyncioSink(asyncio.get_running_loop(), events)
    event = WakeWordEvent(epoch=5, phrase="hey jarvis", score=0.9, detected_at=1.0)

    thread = threading.Thread(target=sink, args=(event,))
    thread.start()
    thread.join(timeout=5.0)

    assert await asyncio.wait_for(events.get(), timeout=5.0) == event


def test_listener_rejects_a_threshold_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match="threshold"):
        VoiceListener(
            frames=FrameQueue(),
            detector=FakeDetector([]),
            state=VoiceStateMachine(),
            endpointer=make_endpointer(),
            on_wake_word=lambda event: None,
            on_utterance=lambda utterance: None,
            threshold=1.5,
        )


def test_frame_queue_drops_the_oldest_frame_when_it_overflows() -> None:
    frames = FrameQueue(maxsize=2)

    for value in (1, 2, 3):
        frames.put(make_frame(value=value), generation=7)

    first, second = frames.get(0.1), frames.get(0.1)

    assert frames.dropped == 1
    assert first is not None
    assert second is not None
    assert (int(first.samples[0]), int(second.samples[0])) == (2, 3)
    assert (first.generation, second.generation) == (7, 7)
    assert frames.get(0.01) is None


def test_state_machine_lets_only_one_trigger_start_listening() -> None:
    state = VoiceStateMachine()

    assert state.try_begin_listening() is True
    assert state.try_begin_listening() is False
    assert state.wake_word_enabled is False


def test_state_machine_bumps_the_generation_on_every_real_transition() -> None:
    state = VoiceStateMachine()
    start = state.generation

    assert state.try_begin_listening() is True
    assert state.generation == start + 1

    state.set(VoiceState.LISTENING)
    assert state.generation == start + 1

    state.set(VoiceState.SPEAKING)
    state.set(VoiceState.IDLE)

    assert state.generation == start + 3
    assert state.snapshot() == (VoiceState.IDLE, start + 3)


def test_state_machine_refuses_a_transition_from_another_state() -> None:
    state = VoiceStateMachine(VoiceState.SPEAKING)
    generation = state.generation

    assert state.try_transition(VoiceState.LISTENING, VoiceState.IDLE) is False
    assert state.state is VoiceState.SPEAKING
    assert state.generation == generation

    assert state.try_transition(VoiceState.SPEAKING, VoiceState.IDLE) is True
    assert state.snapshot() == (VoiceState.IDLE, generation + 1)


def test_the_connection_gate_invalidates_the_frames_captured_around_it() -> None:
    state = VoiceStateMachine()
    gate = ConnectionGate(state, opened=True)
    generation = state.generation

    gate.close()
    assert state.generation == generation + 1

    gate.close()
    assert state.generation == generation + 1

    gate.open()
    assert state.generation == generation + 2


def test_the_connection_gate_leaves_the_generation_alone_while_speaking() -> None:
    state = VoiceStateMachine(VoiceState.SPEAKING)
    gate = ConnectionGate(state, opened=True)
    generation = state.generation

    gate.close()
    gate.open()

    assert state.generation == generation
    assert gate.is_open is True


def wake_up(
    detector: FakeDetector,
    *,
    endpointer: Endpointer | None = None,
    epoch_provider: EpochProvider = unset_epoch,
) -> tuple[VoiceListener, VoiceStateMachine, list[Utterance]]:
    utterances: list[Utterance] = []
    listener, _, state = make_listener(
        detector,
        endpointer=endpointer,
        utterances=utterances,
        epoch_provider=epoch_provider,
    )
    feed_now(listener, state)
    assert state.state is VoiceState.LISTENING
    return listener, state, utterances


def test_listener_records_the_utterance_after_the_wake_word() -> None:
    listener, state, utterances = wake_up(FakeDetector([0.9]))

    feed_now(listener, state, value=SPEECH_RMS)
    assert utterances == []

    feed_now(listener, state, value=SILENCE_RMS)
    feed_now(listener, state, value=SILENCE_RMS)
    feed_now(listener, state, value=SILENCE_RMS)
    feed_now(listener, state, value=SILENCE_RMS)

    assert len(utterances) == 1
    assert utterances[0].outcome is EndpointOutcome.SPEECH
    assert utterances[0].samples.size > 0
    assert state.state is VoiceState.THINKING


def test_listener_reports_a_false_trigger_when_no_speech_follows() -> None:
    listener, state, utterances = wake_up(FakeDetector([0.9]))

    feed_now(listener, state, value=SILENCE_RMS)
    feed_now(listener, state, value=SILENCE_RMS)
    feed_now(listener, state, value=SILENCE_RMS)

    assert len(utterances) == 1
    assert utterances[0].outcome is EndpointOutcome.NO_SPEECH
    assert utterances[0].samples.size == 0
    assert state.state is VoiceState.THINKING


def test_listener_does_not_feed_the_detector_while_recording() -> None:
    detector = FakeDetector([0.9])
    listener, state, _ = wake_up(detector)
    recorded = len(detector.chunks)

    feed_now(listener, state, value=SPEECH_RMS)

    assert len(detector.chunks) == recorded


def test_listener_keeps_the_epoch_captured_at_the_wake_word() -> None:
    epoch = 11
    listener, state, utterances = wake_up(FakeDetector([0.9]), epoch_provider=lambda: epoch)

    feed_now(listener, state, value=SPEECH_RMS)
    for _ in range(4):
        feed_now(listener, state, value=SILENCE_RMS)

    assert [utterance.epoch for utterance in utterances] == [epoch]


def test_listener_drops_the_utterance_when_the_connection_reconnected_meanwhile() -> None:
    epoch = 11

    def current_epoch() -> int:
        return epoch

    listener, state, utterances = wake_up(FakeDetector([0.9]), epoch_provider=current_epoch)

    feed_now(listener, state, value=SPEECH_RMS)
    epoch = 12
    for _ in range(4):
        feed_now(listener, state, value=SILENCE_RMS)

    assert utterances == []
    assert state.state is VoiceState.IDLE


def test_listener_truncates_an_utterance_that_never_ends() -> None:
    endpointer = make_endpointer(silence_s=1.0, start_timeout_s=0.0005, max_duration_s=0.001)
    listener, state, utterances = wake_up(FakeDetector([0.9]), endpointer=endpointer)

    for _ in range(4):
        feed_now(listener, state, value=SPEECH_RMS)

    assert len(utterances) == 1
    assert utterances[0].truncated is True
    assert utterances[0].outcome is EndpointOutcome.SPEECH


def test_listener_forgets_a_half_recorded_utterance_when_the_state_changes() -> None:
    listener, state, utterances = wake_up(FakeDetector([0.9, 0.9]))

    feed_now(listener, state, value=SPEECH_RMS)
    state.set(VoiceState.IDLE)
    feed_now(listener, state, value=SILENCE_RMS)

    assert utterances == []
