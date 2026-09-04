from __future__ import annotations

import asyncio
import contextlib

from apps.voice.audio import FrameQueue
from apps.voice.capture import AudioCapture, resolve_device
from apps.voice.config import VoiceSettings, get_voice_settings
from apps.voice.cues import BeepCue
from apps.voice.listener import AsyncioSink, Utterance, VoiceListener, WakeWordEvent
from apps.voice.pipeline import TRANSCRIPT_QUEUE_MAXSIZE, Transcript, VoicePipeline
from apps.voice.state import VoiceStateMachine
from apps.voice.stt import FasterWhisperSTT
from apps.voice.vad import Endpointer
from apps.voice.wake_word import OpenWakeWordDetector
from libs.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


async def announce_wake_words(events: asyncio.Queue[WakeWordEvent], frames: FrameQueue) -> None:
    while True:
        event = await events.get()
        logger.info(
            "voice.listening_started",
            phrase=event.phrase,
            score=round(event.score, 3),
            epoch=event.epoch,
            dropped_frames=frames.dropped,
        )


async def drain_transcripts(transcripts: asyncio.Queue[Transcript]) -> None:
    while True:
        transcript = await transcripts.get()
        logger.info(
            "voice.transcript",
            epoch=transcript.epoch,
            language=transcript.language,
            text=transcript.text,
        )


async def run(settings: VoiceSettings) -> None:
    loop = asyncio.get_running_loop()
    frames = FrameQueue()
    state = VoiceStateMachine()
    events: asyncio.Queue[WakeWordEvent] = asyncio.Queue()
    utterances: asyncio.Queue[Utterance] = asyncio.Queue()
    transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=TRANSCRIPT_QUEUE_MAXSIZE)

    listener = VoiceListener(
        frames=frames,
        detector=OpenWakeWordDetector.from_settings(settings),
        endpointer=Endpointer.from_settings(settings),
        state=state,
        on_wake_word=AsyncioSink(loop, events),
        on_utterance=AsyncioSink(loop, utterances),
        threshold=settings.wake_word_threshold,
    )
    capture = AudioCapture(
        frames,
        generation_provider=lambda: state.generation,
        device=resolve_device(settings.input_device),
    )
    pipeline = VoicePipeline(
        utterances=utterances,
        transcripts=transcripts,
        stt=FasterWhisperSTT.from_settings(settings),
        state=state,
        cue=BeepCue(),
    )

    try:
        listener.start()
        capture.start()
        logger.info(
            "voice.listening",
            phrase=settings.wake_word_phrase,
            threshold=settings.wake_word_threshold,
        )
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(announce_wake_words(events, frames))
            tasks.create_task(pipeline.run())
            tasks.create_task(drain_transcripts(transcripts))
    finally:
        capture.stop()
        listener.stop()


def main() -> None:
    settings = get_voice_settings()
    configure_logging(level=settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
