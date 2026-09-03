from __future__ import annotations

import asyncio
import contextlib

from apps.voice.audio import FrameQueue
from apps.voice.capture import AudioCapture, resolve_device
from apps.voice.config import VoiceSettings, get_voice_settings
from apps.voice.listener import AsyncioWakeWordSink, WakeWordEvent, WakeWordListener
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.wake_word import OpenWakeWordDetector
from libs.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

WAKE_WORD_COOLDOWN_S = 1.5


async def run(settings: VoiceSettings) -> None:
    frames = FrameQueue()
    state = VoiceStateMachine()
    detector = OpenWakeWordDetector.from_settings(settings)
    events: asyncio.Queue[WakeWordEvent] = asyncio.Queue()
    listener = WakeWordListener(
        frames=frames,
        detector=detector,
        state=state,
        on_wake_word=AsyncioWakeWordSink(asyncio.get_running_loop(), events),
        threshold=settings.wake_word_threshold,
    )
    capture = AudioCapture(
        frames,
        generation_provider=lambda: state.generation,
        device=resolve_device(settings.input_device),
    )

    try:
        listener.start()
        capture.start()
        logger.info(
            "voice.listening",
            phrase=settings.wake_word_phrase,
            threshold=settings.wake_word_threshold,
        )
        while True:
            event = await events.get()
            logger.info(
                "voice.wake_word",
                phrase=event.phrase,
                score=round(event.score, 3),
                epoch=event.epoch,
                dropped_frames=frames.dropped,
            )
            await asyncio.sleep(WAKE_WORD_COOLDOWN_S)
            state.set(VoiceState.IDLE)
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
