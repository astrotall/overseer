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
from apps.voice.listener import UNSET_EPOCH
from apps.voice.pipeline import Transcript
from apps.voice.state import ConnectionGate, VoiceState, VoiceStateMachine
from libs.core.logging import get_logger
from libs.schemas.chat import SendMessageRequest
from libs.schemas.ws import WSIncomingMessage, WSReplyMessage, WSServerMessage

logger = get_logger(__name__)

RECONNECT_INITIAL_S: Final[float] = 1.0
RECONNECT_MAX_S: Final[float] = 30.0
BACKOFF_FACTOR: Final[float] = 2.0
ERROR_SPEECH: Final[str] = "Не удалось получить ответ от агента."
TRANSPORT_ERRORS: Final[tuple[type[Exception], ...]] = (OSError, TimeoutError, WebSocketException)

SERVER_MESSAGE: Final[TypeAdapter[WSServerMessage]] = TypeAdapter(WSServerMessage)


class Speaker(Protocol):
    async def speak(self, text: str) -> bool: ...


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
    ) -> None:
        if reconnect_initial_s <= 0.0:
            raise ValueError(f"reconnect_initial_s must be positive, got {reconnect_initial_s}")
        if reconnect_max_s < reconnect_initial_s:
            raise ValueError(
                f"reconnect_max_s must not be below reconnect_initial_s, got "
                f"{reconnect_max_s} < {reconnect_initial_s}"
            )

        self._url = build_url(url, conversation_id)
        self._transcripts = transcripts
        self._speaker = speaker
        self._state = state
        self._gate = gate if gate is not None else ConnectionGate()
        self._connector = connector
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._epoch = UNSET_EPOCH
        self._awaiting_reply = False

    @classmethod
    def from_settings(
        cls,
        settings: VoiceSettings,
        *,
        transcripts: asyncio.Queue[Transcript],
        speaker: Speaker,
        state: VoiceStateMachine,
    ) -> VoiceWSClient:
        return cls(
            url=settings.ws_url,
            transcripts=transcripts,
            speaker=speaker,
            state=state,
            conversation_id=settings.conversation_id,
            reconnect_initial_s=settings.ws_reconnect_initial_s,
            reconnect_max_s=settings.ws_reconnect_max_s,
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

    def _next_epoch(self) -> int:
        self._epoch += 1
        return self._epoch

    def _release_turn(self) -> None:
        if self._state.state is VoiceState.THINKING:
            self._state.set(VoiceState.IDLE)
