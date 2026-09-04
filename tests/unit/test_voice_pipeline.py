from __future__ import annotations

import asyncio

import numpy as np
import pytest

from apps.voice.audio import Int16Frame
from apps.voice.cues import CueKind
from apps.voice.listener import Utterance
from apps.voice.pipeline import TRANSCRIPT_QUEUE_MAXSIZE, Transcript, VoicePipeline
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.stt import Transcription
from apps.voice.vad import EndpointOutcome

MAX_MESSAGE_LENGTH = 8000


class FakeSTT:
    def __init__(self, *results: Transcription | Exception) -> None:
        self._results = list(results)
        self.calls: list[Int16Frame] = []

    async def transcribe(self, samples: Int16Frame) -> Transcription:
        self.calls.append(samples)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result

        return result


class FakeCue:
    def __init__(self, *, fails: bool = False) -> None:
        self.played: list[CueKind] = []
        self._fails = fails

    async def play(self, kind: CueKind) -> None:
        self.played.append(kind)
        if self._fails:
            raise RuntimeError("нет устройства вывода")


def transcription(
    text: str,
    *,
    no_speech_prob: float = 0.05,
    avg_logprob: float = -0.2,
) -> Transcription:
    return Transcription(
        text=text,
        language="ru",
        no_speech_prob=no_speech_prob,
        avg_logprob=avg_logprob,
    )


def utterance(
    *,
    epoch: int = 0,
    outcome: EndpointOutcome = EndpointOutcome.SPEECH,
    duration_s: float = 2.0,
) -> Utterance:
    samples = (
        np.ones(16_000, dtype=np.int16)
        if outcome is EndpointOutcome.SPEECH
        else np.empty(0, dtype=np.int16)
    )
    return Utterance(
        epoch=epoch,
        outcome=outcome,
        samples=samples,
        duration_s=duration_s,
        truncated=False,
    )


def make_pipeline(
    stt: FakeSTT,
    *,
    cue: FakeCue | None = None,
    state: VoiceStateMachine | None = None,
    epoch_provider: object = None,
) -> tuple[VoicePipeline, asyncio.Queue[Transcript], FakeCue, VoiceStateMachine]:
    pipeline, transcripts, cue, state, _ = build_pipeline(
        stt, cue=cue, state=state, epoch_provider=epoch_provider
    )
    return pipeline, transcripts, cue, state


def build_pipeline(
    stt: FakeSTT,
    *,
    cue: FakeCue | None = None,
    state: VoiceStateMachine | None = None,
    epoch_provider: object = None,
) -> tuple[
    VoicePipeline,
    asyncio.Queue[Transcript],
    FakeCue,
    VoiceStateMachine,
    asyncio.Queue[Utterance],
]:
    cue = cue or FakeCue()
    state = state or VoiceStateMachine(VoiceState.THINKING)
    transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=TRANSCRIPT_QUEUE_MAXSIZE)
    utterances: asyncio.Queue[Utterance] = asyncio.Queue()
    pipeline = VoicePipeline(
        utterances=utterances,
        transcripts=transcripts,
        stt=stt,
        state=state,
        cue=cue,
        **({"epoch_provider": epoch_provider} if epoch_provider is not None else {}),
    )
    return pipeline, transcripts, cue, state, utterances


async def test_pipeline_publishes_the_recognised_text() -> None:
    stt = FakeSTT(transcription("какая погода в Москве"))
    pipeline, transcripts, cue, state = make_pipeline(stt)

    await pipeline.handle(utterance())

    transcript = transcripts.get_nowait()
    assert transcript.text == "какая погода в Москве"
    assert transcript.language == "ru"
    assert transcript.epoch == 0
    assert cue.played == []
    assert state.state is VoiceState.IDLE


async def test_pipeline_never_sends_a_silent_utterance_to_the_model() -> None:
    stt = FakeSTT()
    pipeline, transcripts, cue, state = make_pipeline(stt)

    await pipeline.handle(utterance(outcome=EndpointOutcome.NO_SPEECH))

    assert stt.calls == []
    assert transcripts.empty()
    assert cue.played == [CueKind.NOT_UNDERSTOOD]
    assert state.state is VoiceState.IDLE


@pytest.mark.parametrize("text", ["", "   ", "...", "а"])
async def test_pipeline_drops_an_empty_or_garbled_transcript(text: str) -> None:
    stt = FakeSTT(transcription(text))
    pipeline, transcripts, cue, state = make_pipeline(stt)

    await pipeline.handle(utterance())

    assert transcripts.empty()
    assert cue.played == [CueKind.NOT_UNDERSTOOD]
    assert state.state is VoiceState.IDLE


async def test_pipeline_drops_a_transcript_the_model_believes_is_silence() -> None:
    stt = FakeSTT(transcription("Продолжение следует...", no_speech_prob=0.95))
    pipeline, transcripts, cue, _ = make_pipeline(stt)

    await pipeline.handle(utterance())

    assert transcripts.empty()
    assert cue.played == [CueKind.NOT_UNDERSTOOD]


async def test_pipeline_survives_a_broken_stt() -> None:
    stt = FakeSTT(RuntimeError("модель не загрузилась"))
    pipeline, transcripts, cue, state = make_pipeline(stt)

    await pipeline.handle(utterance())

    assert transcripts.empty()
    assert cue.played == [CueKind.NOT_UNDERSTOOD]
    assert state.state is VoiceState.IDLE


async def test_pipeline_returns_to_idle_even_when_the_cue_itself_fails() -> None:
    stt = FakeSTT(transcription(""))
    pipeline, _, cue, state = make_pipeline(stt, cue=FakeCue(fails=True))

    await pipeline.handle(utterance())

    assert cue.played == [CueKind.NOT_UNDERSTOOD]
    assert state.state is VoiceState.IDLE


async def test_pipeline_rejects_a_transcript_longer_than_the_ws_envelope_allows() -> None:
    stt = FakeSTT(transcription("а" * (MAX_MESSAGE_LENGTH + 1)))
    pipeline, transcripts, cue, _ = make_pipeline(stt)

    await pipeline.handle(utterance())

    assert transcripts.empty()
    assert cue.played == [CueKind.NOT_UNDERSTOOD]


async def test_pipeline_drops_a_transcript_from_a_connection_that_has_reconnected() -> None:
    stt = FakeSTT(transcription("удали черновик"))
    pipeline, transcripts, cue, state = make_pipeline(stt, epoch_provider=lambda: 9)

    await pipeline.handle(utterance(epoch=8))

    assert transcripts.empty()
    assert cue.played == []
    assert state.state is VoiceState.IDLE


async def test_pipeline_keeps_a_transcript_whose_epoch_still_matches() -> None:
    stt = FakeSTT(transcription("удали черновик"))
    pipeline, transcripts, _, _ = make_pipeline(stt, epoch_provider=lambda: 8)

    await pipeline.handle(utterance(epoch=8))

    assert transcripts.get_nowait().text == "удали черновик"


async def test_pipeline_drops_a_transcript_when_nobody_drained_the_queue() -> None:
    stt = FakeSTT(transcription("первая"), transcription("вторая"))
    pipeline, transcripts, _, _ = make_pipeline(stt)

    await pipeline.handle(utterance())
    await pipeline.handle(utterance())

    assert transcripts.get_nowait().text == "первая"
    assert transcripts.empty()


async def test_pipeline_run_consumes_utterances_until_cancelled() -> None:
    stt = FakeSTT(transcription("первая"), transcription("вторая"))
    pipeline, transcripts, _, _, utterances = build_pipeline(stt)

    task = asyncio.create_task(pipeline.run())
    try:
        utterances.put_nowait(utterance())
        assert (await asyncio.wait_for(transcripts.get(), timeout=5.0)).text == "первая"
        utterances.put_nowait(utterance())
        assert (await asyncio.wait_for(transcripts.get(), timeout=5.0)).text == "вторая"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_the_cue_is_audible_and_fits_int16() -> None:
    from apps.voice.audio import SAMPLE_RATE
    from apps.voice.cues import CUE_TONE_S, NOT_UNDERSTOOD_TONES, render_cue

    cue = render_cue(CueKind.NOT_UNDERSTOOD)

    assert cue.dtype == np.int16
    assert cue.size == int(SAMPLE_RATE * CUE_TONE_S) * len(NOT_UNDERSTOOD_TONES)
    assert np.abs(cue).max() > 1000


def test_the_cue_fades_in_and_out_so_it_does_not_click() -> None:
    from apps.voice.cues import render_cue

    cue = render_cue(CueKind.NOT_UNDERSTOOD)

    assert abs(int(cue[0])) < 100
    assert abs(int(cue[-1])) < 100


async def test_the_log_cue_is_a_silent_stand_in() -> None:
    from apps.voice.cues import LogCue

    await LogCue().play(CueKind.NOT_UNDERSTOOD)
