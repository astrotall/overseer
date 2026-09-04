from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from apps.voice.cues import Cue, CueKind
from apps.voice.listener import EpochProvider, Utterance, unset_epoch
from apps.voice.playback import PlaybackOutcome, SpeechPlayer
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.stt import SpeechToText, is_meaningful, transcript_quality
from apps.voice.tts import TextToSpeech, is_speakable
from apps.voice.vad import EndpointOutcome
from libs.core.logging import get_logger
from libs.schemas.chat import SendMessageRequest

logger = get_logger(__name__)

TRANSCRIPT_QUEUE_MAXSIZE: Final[int] = 1


@dataclass(frozen=True, slots=True)
class Transcript:
    epoch: int
    text: str
    language: str | None
    duration_s: float


class VoicePipeline:
    def __init__(
        self,
        *,
        utterances: asyncio.Queue[Utterance],
        transcripts: asyncio.Queue[Transcript],
        stt: SpeechToText,
        state: VoiceStateMachine,
        cue: Cue,
        epoch_provider: EpochProvider = unset_epoch,
    ) -> None:
        self._utterances = utterances
        self._transcripts = transcripts
        self._stt = stt
        self._state = state
        self._cue = cue
        self._epoch_provider = epoch_provider

    async def run(self) -> None:
        while True:
            utterance = await self._utterances.get()
            await self.handle(utterance)

    async def handle(self, utterance: Utterance) -> None:
        published = False
        try:
            transcript = await self._transcribe(utterance)
            if transcript is not None:
                published = self._publish(transcript)
        finally:
            if not published:
                self._state.set(VoiceState.IDLE)

    async def _transcribe(self, utterance: Utterance) -> Transcript | None:
        if utterance.outcome is EndpointOutcome.NO_SPEECH:
            logger.info("voice.utterance_silent", epoch=utterance.epoch)
            await self._notify(CueKind.NOT_UNDERSTOOD)
            return None

        try:
            transcription = await self._stt.transcribe(utterance.samples)
        except Exception:
            logger.exception("voice.stt_failed", epoch=utterance.epoch)
            await self._notify(CueKind.NOT_UNDERSTOOD)
            return None

        if not is_meaningful(transcription):
            quality = transcript_quality(transcription)
            logger.info(
                "voice.transcript_rejected",
                epoch=utterance.epoch,
                language=transcription.language,
                chars=len(transcription.text),
                segments=quality.segments,
                speech_segments=quality.speech_segments,
                trimmed=quality.trimmed,
                no_speech_prob=round(quality.no_speech_prob, 3),
                avg_logprob=round(quality.avg_logprob, 3),
            )
            await self._notify(CueKind.NOT_UNDERSTOOD)
            return None

        try:
            content = SendMessageRequest(content=transcription.text).content
        except ValidationError as exc:
            logger.info(
                "voice.transcript_invalid",
                epoch=utterance.epoch,
                chars=len(transcription.text),
                errors=[error["type"] for error in exc.errors()],
            )
            await self._notify(CueKind.NOT_UNDERSTOOD)
            return None

        return Transcript(
            epoch=utterance.epoch,
            text=content,
            language=transcription.language,
            duration_s=utterance.duration_s,
        )

    def _publish(self, transcript: Transcript) -> bool:
        current = self._epoch_provider()
        if transcript.epoch != current:
            logger.debug(
                "voice.transcript_dropped_stale_epoch",
                epoch=transcript.epoch,
                current=current,
            )
            return False

        try:
            self._transcripts.put_nowait(transcript)
        except asyncio.QueueFull:
            logger.warning("voice.transcript_dropped_queue_full", epoch=transcript.epoch)
            return False

        logger.info(
            "voice.transcript_ready",
            epoch=transcript.epoch,
            language=transcript.language,
            duration_s=round(transcript.duration_s, 2),
            chars=len(transcript.text),
        )
        return True

    async def _notify(self, kind: CueKind) -> None:
        self._state.set(VoiceState.SPEAKING)
        try:
            await self._cue.play(kind)
        except Exception:
            logger.exception("voice.cue_failed", kind=kind.value)


class VoiceSpeaker:
    def __init__(
        self,
        *,
        tts: TextToSpeech,
        player: SpeechPlayer,
        state: VoiceStateMachine,
    ) -> None:
        self._tts = tts
        self._player = player
        self._state = state
        self._turn = asyncio.Lock()

    async def speak(self, text: str) -> bool:
        if not is_speakable(text):
            logger.info("voice.speak_skipped", chars=len(text))
            return False

        async with self._turn:
            return await self._speak(text)

    async def _speak(self, text: str) -> bool:
        self._state.set(VoiceState.SPEAKING)
        try:
            speech = await self._tts.synthesize(text)
            outcome = await self._player.play(speech)
        except Exception:
            logger.exception("voice.speak_failed", chars=len(text))
            return False
        else:
            logger.info(
                "voice.spoken",
                chars=len(text),
                outcome=outcome.value,
                duration_s=round(speech.duration_s, 2),
            )
            return outcome is PlaybackOutcome.PLAYED
        finally:
            self._state.set(VoiceState.IDLE)
