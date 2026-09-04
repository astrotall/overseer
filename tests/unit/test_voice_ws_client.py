from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from apps.voice.pipeline import Transcript
from apps.voice.state import VoiceState, VoiceStateMachine
from apps.voice.ws_client import (
    ERROR_SPEECH,
    RECONNECT_MAX_S,
    VoiceWSClient,
    WSConnection,
    build_url,
    next_delay,
)

URL = "ws://localhost:8000/ws/chat"
FIRST_EPOCH = 1
SECOND_EPOCH = 3


class FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        return True


class FakeConnection:
    def __init__(
        self,
        *incoming: str | BaseException,
        answer: bool = False,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._incoming = list(incoming)
        self._answer = answer
        self._gate = gate if gate is not None else asyncio.Event()
        if not answer and gate is None:
            self._gate.set()

    async def send(self, message: str) -> None:
        self.sent.append(message)
        if self._answer:
            self._gate.set()

    async def recv(self) -> str | bytes:
        await self._gate.wait()
        if self._incoming:
            item = self._incoming.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    @property
    def envelopes(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in self.sent]


class FakeConnector:
    def __init__(self, *connections: FakeConnection | BaseException) -> None:
        self.urls: list[str] = []
        self._connections = list(connections)

    def __call__(self, url: str) -> Any:
        self.urls.append(url)

        @asynccontextmanager
        async def session() -> AsyncIterator[WSConnection]:
            if not self._connections:
                await asyncio.Event().wait()

            connection = self._connections.pop(0)
            if isinstance(connection, BaseException):
                raise connection

            try:
                yield connection
            finally:
                connection.closed = True

        return session()


def transcript(*, epoch: int = FIRST_EPOCH, text: str = "какая погода в москве") -> Transcript:
    return Transcript(epoch=epoch, text=text, language="ru", duration_s=2.0)


def reply(content: str | None = "В Москве плюс семь и дождь.") -> str:
    return json.dumps({"type": "reply", "payload": {"role": "assistant", "content": content}})


def error(code: int = 503) -> str:
    return json.dumps(
        {
            "type": "error",
            "payload": {
                "error": "LLMTransientError",
                "detail": "провайдер недоступен",
                "code": code,
            },
        }
    )


def make_client(
    connector: FakeConnector,
    *,
    transcripts: asyncio.Queue[Transcript] | None = None,
    speaker: FakeSpeaker | None = None,
    state: VoiceStateMachine | None = None,
) -> VoiceWSClient:
    return VoiceWSClient(
        url=URL,
        transcripts=transcripts if transcripts is not None else asyncio.Queue(maxsize=1),
        speaker=speaker or FakeSpeaker(),
        state=state or VoiceStateMachine(),
        connector=connector,
        reconnect_initial_s=0.001,
        reconnect_max_s=0.002,
    )


async def eventually(predicate: Callable[[], object], *, limit_s: float = 2.0) -> None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)

    raise AssertionError("условие не наступило за отведённое время")


@asynccontextmanager
async def running(client: VoiceWSClient) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(client.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestUrl:
    def test_without_a_conversation_the_url_is_left_alone(self) -> None:
        assert build_url(URL, None) == URL

    def test_a_conversation_goes_into_the_query(self) -> None:
        conversation_id = uuid.uuid4()

        assert build_url(URL, conversation_id) == f"{URL}?conversation_id={conversation_id}"

    def test_an_existing_query_survives(self) -> None:
        conversation_id = uuid.uuid4()

        assert build_url(f"{URL}?token=abc", conversation_id) == (
            f"{URL}?token=abc&conversation_id={conversation_id}"
        )


class TestBackoff:
    def test_the_delay_doubles(self) -> None:
        assert next_delay(1.0, RECONNECT_MAX_S) == 2.0

    def test_the_delay_stops_at_the_ceiling(self) -> None:
        assert next_delay(20.0, RECONNECT_MAX_S) == RECONNECT_MAX_S

    def test_a_ceiling_below_the_first_delay_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reconnect_max_s"):
            VoiceWSClient(
                url=URL,
                transcripts=asyncio.Queue(),
                speaker=FakeSpeaker(),
                state=VoiceStateMachine(),
                connector=FakeConnector(),
                reconnect_initial_s=5.0,
                reconnect_max_s=1.0,
            )


class TestSending:
    async def test_a_transcript_of_the_current_epoch_is_sent_as_a_message_envelope(self) -> None:
        connection = FakeConnection()
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        client = make_client(FakeConnector(connection), transcripts=transcripts)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: connection.sent)

        assert connection.envelopes == [
            {"type": "message", "payload": {"content": "какая погода в москве"}}
        ]

    async def test_a_transcript_of_a_stale_epoch_is_never_sent(self) -> None:
        connection = FakeConnection()
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        state = VoiceStateMachine()
        client = make_client(
            FakeConnector(connection), transcripts=transcripts, speaker=speaker, state=state
        )

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            state.set(VoiceState.THINKING)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH + 7))
            await eventually(lambda: transcripts.empty())
            await eventually(lambda: state.state is VoiceState.IDLE)

        assert connection.sent == []
        assert speaker.spoken == []

    async def test_a_transcript_captured_before_the_socket_came_up_is_dropped(self) -> None:
        connection = FakeConnection()
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        transcripts.put_nowait(transcript(epoch=0))
        client = make_client(FakeConnector(connection), transcripts=transcripts)

        async with running(client):
            await eventually(lambda: transcripts.empty())
            await asyncio.sleep(0.01)

        assert connection.sent == []


class TestReceiving:
    async def test_a_reply_is_spoken(self) -> None:
        connection = FakeConnection(reply(), answer=True)
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        client = make_client(FakeConnector(connection), transcripts=transcripts, speaker=speaker)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: speaker.spoken)

        assert speaker.spoken == ["В Москве плюс семь и дождь."]

    async def test_a_reply_returns_the_state_from_thinking_to_idle(self) -> None:
        connection = FakeConnection(reply(), answer=True)
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        state = VoiceStateMachine()
        client = make_client(
            FakeConnector(connection), transcripts=transcripts, speaker=speaker, state=state
        )

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            state.set(VoiceState.THINKING)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: state.state is VoiceState.IDLE)

        assert speaker.spoken == ["В Москве плюс семь и дождь."]

    async def test_an_error_envelope_is_spoken_instead_of_silence(self) -> None:
        connection = FakeConnection(error(), answer=True)
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        client = make_client(FakeConnector(connection), transcripts=transcripts, speaker=speaker)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: speaker.spoken)

        assert speaker.spoken == [ERROR_SPEECH]

    async def test_an_empty_reply_is_not_spoken(self) -> None:
        connection = FakeConnection(reply(None), answer=True)
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        state = VoiceStateMachine()
        client = make_client(
            FakeConnector(connection), transcripts=transcripts, speaker=speaker, state=state
        )

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            state.set(VoiceState.THINKING)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: state.state is VoiceState.IDLE)

        assert speaker.spoken == []

    async def test_an_unsolicited_error_is_not_spoken(self) -> None:
        connection = FakeConnection(error(code=404))
        speaker = FakeSpeaker()
        client = make_client(FakeConnector(connection, FakeConnection()), speaker=speaker)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            await asyncio.sleep(0.02)

        assert speaker.spoken == []

    async def test_an_unreadable_message_does_not_break_the_connection(self) -> None:
        connection = FakeConnection("не json вовсе", reply(), answer=True)
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        speaker = FakeSpeaker()
        client = make_client(FakeConnector(connection), transcripts=transcripts, speaker=speaker)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            transcripts.put_nowait(transcript(epoch=FIRST_EPOCH))
            await eventually(lambda: speaker.spoken)

            assert speaker.spoken == ["В Москве плюс семь и дождь."]
            assert client.epoch == FIRST_EPOCH
            assert not connection.closed


class TestReconnect:
    async def test_the_client_survives_a_dropped_connection_and_reconnects(self) -> None:
        first = FakeConnection(ConnectionResetError("соединение сброшено"))
        second = FakeConnection()
        transcripts: asyncio.Queue[Transcript] = asyncio.Queue(maxsize=1)
        client = make_client(FakeConnector(first, second), transcripts=transcripts)

        async with running(client) as task:
            await eventually(lambda: client.epoch == SECOND_EPOCH)
            transcripts.put_nowait(transcript(epoch=SECOND_EPOCH))
            await eventually(lambda: second.sent)

            assert not task.done()

        assert first.sent == []
        assert second.envelopes == [
            {"type": "message", "payload": {"content": "какая погода в москве"}}
        ]

    async def test_a_failed_handshake_is_retried(self) -> None:
        connection = FakeConnection()
        connector = FakeConnector(ConnectionRefusedError("api не поднят"), connection)
        client = make_client(connector)

        async with running(client) as task:
            await eventually(lambda: client.epoch == FIRST_EPOCH)

            assert not task.done()

        assert connector.urls == [URL, URL]

    async def test_an_unexpected_failure_does_not_kill_the_client(self) -> None:
        first = FakeConnection(RuntimeError("движок сломался"))
        second = FakeConnection()
        client = make_client(FakeConnector(first, second))

        async with running(client) as task:
            await eventually(lambda: client.epoch == SECOND_EPOCH)

            assert not task.done()

    async def test_a_reconnect_leaves_no_turn_hanging_in_thinking(self) -> None:
        state = VoiceStateMachine()
        drop = asyncio.Event()
        connector = FakeConnector(
            FakeConnection(ConnectionResetError(), gate=drop),
            FakeConnection(gate=asyncio.Event()),
        )
        client = make_client(connector, state=state)

        async with running(client):
            await eventually(lambda: client.epoch == FIRST_EPOCH)
            state.set(VoiceState.THINKING)
            drop.set()
            await eventually(lambda: state.state is VoiceState.IDLE)
            await eventually(lambda: client.epoch == SECOND_EPOCH)

    async def test_every_connection_gets_its_own_epoch(self) -> None:
        drops = [asyncio.Event(), asyncio.Event()]
        connector = FakeConnector(
            FakeConnection(ConnectionResetError(), gate=drops[0]),
            FakeConnection(ConnectionResetError(), gate=drops[1]),
            FakeConnection(gate=asyncio.Event()),
        )
        client = make_client(connector)
        seen: list[int] = []

        async with running(client):
            for drop in drops:
                await eventually(lambda: client.epoch % 2 == 1)
                seen.append(client.epoch)
                drop.set()
                await eventually(lambda: client.epoch % 2 == 0)

            await eventually(lambda: client.epoch % 2 == 1)
            seen.append(client.epoch)

        assert seen == [1, 3, 5]
