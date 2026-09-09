from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Final, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import TypeAdapter, ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from apps.voice.config import VoiceSettings
from apps.voice.confirmations import (
    ConfirmationAPI,
    ConfirmationError,
    ConfirmationGoneError,
    Decision,
    classify,
)
from apps.voice.listener import UNSET_EPOCH
from apps.voice.pipeline import Transcript
from apps.voice.state import ConnectionGate, VoiceState, VoiceStateMachine
from libs.core.logging import get_logger
from libs.schemas.chat import ConfirmationRequiredResponse, SendMessageRequest
from libs.schemas.ws import (
    WSConfirmationRequiredMessage,
    WSIncomingMessage,
    WSReplyMessage,
    WSServerMessage,
)

logger = get_logger(__name__)

RECONNECT_INITIAL_S: Final[float] = 1.0
RECONNECT_MAX_S: Final[float] = 30.0
BACKOFF_FACTOR: Final[float] = 2.0
ERROR_SPEECH: Final[str] = "Не удалось получить ответ от агента."
CONFIRMATION_SPEECH: Final[str] = (
    "Это действие требует подтверждения, а подтвердить его голосом пока нельзя."
)
CONFIRMATION_QUESTION: Final[str] = "{summary} Подтверждаете?"
CONFIRMATION_RETRY: Final[str] = "Не расслышал. Скажите «да» или «нет»."
CONFIRMATION_GAVE_UP_SPEECH: Final[str] = "Так и не понял ответ. Отменяю действие."
CONFIRMATION_FAILED_SPEECH: Final[str] = "Не удалось передать ваш ответ агенту."
CONFIRMATION_GONE_SPEECH: Final[str] = "Подтверждение больше не действует."
CONFIRMATION_CHAIN_SPEECH: Final[str] = "Слишком много подтверждений подряд, останавливаюсь."
CONFIRMATION_ATTEMPTS: Final[int] = 2
CONFIRMATION_CHAIN_LIMIT: Final[int] = 5
ANSWER_TIMEOUT_S: Final[float] = 45.0
TRANSPORT_ERRORS: Final[tuple[type[Exception], ...]] = (OSError, TimeoutError, WebSocketException)

SERVER_MESSAGE: Final[TypeAdapter[WSServerMessage]] = TypeAdapter(WSServerMessage)


class Speaker(Protocol):
    async def speak(self, text: str) -> bool: ...


ListenRequest = Callable[[], bool]


def listening_unavailable() -> bool:
    return False


class WSConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], AbstractAsyncContextManager[WSConnection]]


def websocket_connector(url: str) -> AbstractAsyncContextManager[WSConnection]:
    return connect(url)


def build_url(url: str, conversation_id: uuid.UUID | None) -> str:
    if conversation_id is None:
        return url

    parts = urlsplit(url)
    query = urlencode({"conversation_id": str(conversation_id)})
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            f"{parts.query}&{query}" if parts.query else query,
            parts.fragment,
        )
    )


def next_delay(delay: float, maximum: float) -> float:
    return min(delay * BACKOFF_FACTOR, maximum)


class VoiceWSClient:
    def __init__(
        self,
        *,
        url: str,
        transcripts: asyncio.Queue[Transcript],
        speaker: Speaker,
        state: VoiceStateMachine,
        conversation_id: uuid.UUID | None = None,
        gate: ConnectionGate | None = None,
        connector: Connector = websocket_connector,
        reconnect_initial_s: float = RECONNECT_INITIAL_S,
        reconnect_max_s: float = RECONNECT_MAX_S,
        confirmations: ConfirmationAPI | None = None,
        listen: ListenRequest = listening_unavailable,
        answer_timeout_s: float = ANSWER_TIMEOUT_S,
    ) -> None:
        if reconnect_initial_s <= 0.0:
            raise ValueError(f"reconnect_initial_s must be positive, got {reconnect_initial_s}")
        if reconnect_max_s < reconnect_initial_s:
            raise ValueError(
                f"reconnect_max_s must not be below reconnect_initial_s, got "
                f"{reconnect_max_s} < {reconnect_initial_s}"
            )
        if answer_timeout_s <= 0.0:
            raise ValueError(f"answer_timeout_s must be positive, got {answer_timeout_s}")

        self._url = build_url(url, conversation_id)
        self._transcripts = transcripts
        self._speaker = speaker
        self._state = state
        self._gate = gate if gate is not None else ConnectionGate(state)
        self._connector = connector
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._confirmations = confirmations
        self._listen = listen
        self._answer_timeout_s = answer_timeout_s
        self._epoch = UNSET_EPOCH
        self._awaiting_reply = False
        self._answers: asyncio.Queue[Transcript] = asyncio.Queue()
        self._confirming = False

    @classmethod
    def from_settings(
        cls,
        settings: VoiceSettings,
        *,
        transcripts: asyncio.Queue[Transcript],
        speaker: Speaker,
        state: VoiceStateMachine,
        confirmations: ConfirmationAPI | None = None,
        listen: ListenRequest = listening_unavailable,
    ) -> VoiceWSClient:
        return cls(
            url=settings.ws_url,
            transcripts=transcripts,
            speaker=speaker,
            state=state,
            conversation_id=settings.conversation_id,
            reconnect_initial_s=settings.ws_reconnect_initial_s,
            reconnect_max_s=settings.ws_reconnect_max_s,
            confirmations=confirmations,
            listen=listen,
            answer_timeout_s=settings.confirmation_answer_timeout_s,
        )

    @property
    def gate(self) -> ConnectionGate:
        return self._gate

    @property
    def url(self) -> str:
        return self._url

    @property
    def epoch(self) -> int:
        return self._epoch

    async def run(self) -> None:
        delay = self._reconnect_initial_s
        while True:
            if await self._session():
                delay = self._reconnect_initial_s

            logger.info("voice.ws_reconnecting", delay_s=round(delay, 2), epoch=self._epoch)
            await asyncio.sleep(delay)
            delay = next_delay(delay, self._reconnect_max_s)

    async def _session(self) -> bool:
        connected = False
        try:
            async with self._connector(self._url) as connection:
                connected = True
                self._awaiting_reply = False
                self._next_epoch()
                self._gate.open()
                logger.info("voice.ws_connected", url=self._url, epoch=self._epoch)
                await self._serve(connection)
        except TRANSPORT_ERRORS as exc:
            logger.warning(
                "voice.ws_disconnected",
                epoch=self._epoch,
                error=type(exc).__name__,
                detail=str(exc),
            )
        except Exception:
            logger.exception("voice.ws_failed", epoch=self._epoch)
        else:
            logger.info("voice.ws_closed", epoch=self._epoch)
        finally:
            self._gate.close()
            if connected:
                self._next_epoch()
            self._awaiting_reply = False
            self._release_turn()

        return connected

    async def _serve(self, connection: WSConnection) -> None:
        sender = asyncio.create_task(self._send_loop(connection), name="voice-ws-send")
        try:
            await self._receive_loop(connection)
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def _send_loop(self, connection: WSConnection) -> None:
        while True:
            transcript = await self._transcripts.get()
            if self._confirming:
                self._answers.put_nowait(transcript)
                continue

            await self._send(connection, transcript)

    async def _receive_loop(self, connection: WSConnection) -> None:
        while True:
            await self._handle(await connection.recv())

    async def _send(self, connection: WSConnection, transcript: Transcript) -> None:
        current = self._epoch
        if transcript.epoch != current:
            logger.info(
                "voice.ws_transcript_dropped_stale_epoch",
                epoch=transcript.epoch,
                current=current,
            )
            self._release_turn()
            return

        try:
            envelope = WSIncomingMessage(
                type="message", payload=SendMessageRequest(content=transcript.text)
            )
        except ValidationError as exc:
            logger.warning(
                "voice.ws_transcript_invalid",
                epoch=transcript.epoch,
                chars=len(transcript.text),
                errors=[error["type"] for error in exc.errors()],
            )
            self._release_turn()
            return

        self._awaiting_reply = True
        await connection.send(envelope.model_dump_json())
        logger.info(
            "voice.ws_message_sent",
            epoch=transcript.epoch,
            chars=len(transcript.text),
            duration_s=round(transcript.duration_s, 2),
        )

    async def _handle(self, raw: str | bytes) -> None:
        try:
            message = SERVER_MESSAGE.validate_json(raw)
        except ValidationError as exc:
            logger.warning(
                "voice.ws_unreadable_message",
                epoch=self._epoch,
                errors=[error["type"] for error in exc.errors()],
            )
            return

        if not self._awaiting_reply:
            logger.warning(
                "voice.ws_unsolicited_message", epoch=self._epoch, message_type=message.type
            )
            return

        self._awaiting_reply = False
        try:
            if isinstance(message, WSReplyMessage):
                await self._speak_reply(message.payload.role, message.payload.content)
            elif isinstance(message, WSConfirmationRequiredMessage):
                await self._confirm(message.payload)
            else:
                logger.warning(
                    "voice.ws_error",
                    epoch=self._epoch,
                    code=message.payload.code,
                    error=message.payload.error,
                    detail=message.payload.detail,
                )
                await self._speaker.speak(ERROR_SPEECH)
        finally:
            self._release_turn()

    async def _speak_reply(self, role: str, content: str | None) -> None:
        if content is None:
            logger.warning("voice.ws_reply_empty", epoch=self._epoch, role=role)
            return

        logger.info("voice.ws_reply", epoch=self._epoch, role=role, chars=len(content))
        await self._speaker.speak(content)

    async def _confirm(self, payload: ConfirmationRequiredResponse) -> None:
        logger.info(
            "voice.confirmation_required",
            epoch=self._epoch,
            confirmation_id=str(payload.confirmation_id),
        )
        if self._confirmations is None:
            await self._speaker.speak(CONFIRMATION_SPEECH)
            return

        self._confirming = True
        try:
            pending = payload
            for _ in range(CONFIRMATION_CHAIN_LIMIT):
                decision = await self._ask(pending.summary)
                resumed = await self._resolve(pending.confirmation_id, decision)
                if resumed is None:
                    return

                pending = resumed

            logger.warning("voice.confirmation_chain_too_long", epoch=self._epoch)
            await self._speaker.speak(CONFIRMATION_CHAIN_SPEECH)
        finally:
            self._confirming = False
            self._drain_answers()

    async def _ask(self, summary: str) -> Decision:
        question = CONFIRMATION_QUESTION.format(summary=summary)
        for _ in range(CONFIRMATION_ATTEMPTS):
            await self._speaker.speak(question)
            decision = await self._hear()
            if decision is not Decision.UNCLEAR:
                return decision

            question = CONFIRMATION_RETRY

        logger.info("voice.confirmation_unclear_twice", epoch=self._epoch)
        await self._speaker.speak(CONFIRMATION_GAVE_UP_SPEECH)
        return Decision.REJECT

    async def _hear(self) -> Decision:
        self._drain_answers()
        if not self._listen():
            logger.warning("voice.confirmation_not_listening", epoch=self._epoch)
            return Decision.UNCLEAR

        try:
            answer = await asyncio.wait_for(self._answers.get(), self._answer_timeout_s)
        except TimeoutError:
            logger.warning("voice.confirmation_answer_timed_out", epoch=self._epoch)
            self._state.try_transition(VoiceState.LISTENING, VoiceState.IDLE)
            return Decision.UNCLEAR

        if answer.epoch != self._epoch:
            logger.info(
                "voice.confirmation_answer_dropped_stale_epoch",
                epoch=answer.epoch,
                current=self._epoch,
            )
            return Decision.UNCLEAR

        generation = self._state.generation
        if answer.generation != generation:
            logger.info(
                "voice.confirmation_answer_dropped_stale_generation",
                generation=answer.generation,
                current=generation,
            )
            return Decision.UNCLEAR

        decision = classify(answer.text)
        logger.info(
            "voice.confirmation_answer",
            epoch=answer.epoch,
            decision=decision.value,
            chars=len(answer.text),
        )
        return decision

    async def _resolve(
        self, confirmation_id: uuid.UUID, decision: Decision
    ) -> ConfirmationRequiredResponse | None:
        if self._confirmations is None:
            return None

        try:
            resolution = await self._confirmations.resolve(
                confirmation_id, approved=decision is Decision.CONFIRM
            )
        except ConfirmationGoneError as exc:
            logger.warning(
                "voice.confirmation_gone",
                epoch=self._epoch,
                confirmation_id=str(confirmation_id),
                detail=exc.message,
            )
            await self._speaker.speak(CONFIRMATION_GONE_SPEECH)
            return None
        except ConfirmationError as exc:
            logger.warning(
                "voice.confirmation_not_delivered",
                epoch=self._epoch,
                confirmation_id=str(confirmation_id),
                detail=exc.message,
            )
            await self._speaker.speak(CONFIRMATION_FAILED_SPEECH)
            return None

        if resolution.pending is not None:
            return resolution.pending

        if resolution.reply is None:
            logger.warning("voice.confirmation_reply_empty", epoch=self._epoch)
            return None

        logger.info("voice.confirmation_reply", epoch=self._epoch, chars=len(resolution.reply))
        await self._speaker.speak(resolution.reply)
        return None

    def _drain_answers(self) -> None:
        while True:
            try:
                stale = self._answers.get_nowait()
            except asyncio.QueueEmpty:
                return

            logger.info("voice.confirmation_answer_discarded", epoch=stale.epoch)

    def _next_epoch(self) -> int:
        self._epoch += 1
        return self._epoch

    def _release_turn(self) -> None:
        if self._state.state is VoiceState.THINKING:
            self._state.set(VoiceState.IDLE)
