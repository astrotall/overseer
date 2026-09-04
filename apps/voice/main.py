from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Final

from apps.voice.audio import FrameQueue
from apps.voice.capture import AudioCapture, resolve_device
from apps.voice.config import VoiceSettings, get_voice_settings
from apps.voice.cues import BeepCue
from apps.voice.listener import AsyncioSink, Utterance, VoiceListener, WakeWordEvent
from apps.voice.pipeline import (
    TRANSCRIPT_QUEUE_MAXSIZE,
    Transcript,
    VoicePipeline,
    VoiceSpeaker,
)
from apps.voice.playback import SoundDeviceSink, SpeechPlayer
from apps.voice.state import VoiceStateMachine
from apps.voice.stt import FasterWhisperSTT
from apps.voice.tts import SileroTTS
from apps.voice.vad import Endpointer
from apps.voice.wake_word import OpenWakeWordDetector
from apps.voice.ws_client import VoiceWSClient
from libs.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

SAY_COMMAND: Final[str] = "say"
SAY_DEFAULT_TEXT: Final[str] = "Отчёт за август сформирован и сохранён в документ Word."


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


async def run(settings: VoiceSettings) -> None:
    loop = asyncio.get_running_loop()
    frames = FrameQueue()
    state = VoiceStateMachine()
    events: asyncio.Queue[WakeWordEvent] = asyncio.Queue()
    utterances: asyncio.Queue[Utterance] = asyncio.Queue()
    transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=TRANSCRIPT_QUEUE_MAXSIZE)

    speaker, player = build_speaker(settings, state)
    client = VoiceWSClient.from_settings(
        settings, transcripts=transcripts, speaker=speaker, state=state
    )
    listener = VoiceListener(
        frames=frames,
        detector=OpenWakeWordDetector.from_settings(settings),
        endpointer=Endpointer.from_settings(settings),
        state=state,
        on_wake_word=AsyncioSink(loop, events),
        on_utterance=AsyncioSink(loop, utterances),
        threshold=settings.wake_word_threshold,
        epoch_provider=lambda: client.epoch,
        gate=client.gate,
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
        epoch_provider=lambda: client.epoch,
    )

    try:
        player.start()
        listener.start()
        capture.start()
        logger.info(
            "voice.listening",
            phrase=settings.wake_word_phrase,
            threshold=settings.wake_word_threshold,
            ws_url=client.url,
        )
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(announce_wake_words(events, frames))
            tasks.create_task(pipeline.run())
            tasks.create_task(client.run())
    finally:
        capture.stop()
        listener.stop()
        player.stop()


def build_speaker(
    settings: VoiceSettings, state: VoiceStateMachine
) -> tuple[VoiceSpeaker, SpeechPlayer]:
    tts = SileroTTS.from_settings(settings)
    player = SpeechPlayer(
        sink=SoundDeviceSink(
            samplerate=tts.samplerate,
            device=resolve_device(settings.output_device),
        ),
        generation_provider=lambda: state.generation,
    )
    return VoiceSpeaker(tts=tts, player=player, state=state), player


async def say(settings: VoiceSettings, text: str) -> None:
    state = VoiceStateMachine()
    speaker, player = build_speaker(settings, state)
    player.start()
    try:
        spoken = await speaker.speak(text)
    finally:
        player.stop()

    logger.info("voice.say_finished", spoken=spoken, state=state.state.value, chars=len(text))


def main() -> None:
    settings = get_voice_settings()
    configure_logging(level=settings.log_level)
    argv = sys.argv[1:]
    with contextlib.suppress(KeyboardInterrupt):
        if argv and argv[0] == SAY_COMMAND:
            asyncio.run(say(settings, " ".join(argv[1:]).strip() or SAY_DEFAULT_TEXT))
            return

        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
