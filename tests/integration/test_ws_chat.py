from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.websockets import WebSocketDisconnect

from apps.api.deps import get_active_llm_client
from libs.core.exceptions import LLMTransientError
from libs.db.models import Conversation, Message
from libs.db.repositories import ConversationRepository
from libs.db.session import get_session
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from libs.llm.system_prompt import get_system_prompt_message

OK_RESPONSE = LLMResponse(model="fake-model", stop_reason="end_turn", text="готово")


class FakeLLMClient(LLMClient):
    default_model = "fake-model"

    def __init__(self, outcomes: Sequence[LLMResponse | Exception] | None = None) -> None:
        self.calls: list[list[ChatMessage]] = []
        self._outcomes = list(outcomes) if outcomes is not None else None

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self._outcomes is None:
            return OK_RESPONSE
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _override_session(app: FastAPI, database_url: str) -> None:
    async def _get_session() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(database_url, poolclass=NullPool, future=True)
        try:
            async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as session:
                yield session
        finally:
            await engine.dispose()

    app.dependency_overrides[get_session] = _get_session


def _override_llm_client(app: FastAPI, llm_client: LLMClient) -> None:
    def _get_llm_client() -> LLMClient:
        return llm_client

    app.dependency_overrides[get_active_llm_client] = _get_llm_client


def _message(content: str) -> str:
    return json.dumps({"type": "message", "payload": {"content": content}}, ensure_ascii=False)


async def _delete_conversation(engine: AsyncEngine, conversation_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(Message).where(Message.conversation_id == conversation_id))
        await connection.execute(delete(Conversation).where(Conversation.id == conversation_id))


async def _history(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[ChatMessage]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await ConversationRepository(session).get_history(conversation_id)


@pytest.fixture
def client(app: FastAPI, test_database_url: str) -> TestClient:
    _override_session(app, test_database_url)
    return TestClient(app)


@pytest.fixture
async def conversation_id(db_engine: AsyncEngine) -> AsyncIterator[uuid.UUID]:
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        created = await ConversationRepository(session).create_conversation()
        await session.commit()
    try:
        yield created
    finally:
        await _delete_conversation(db_engine, created)


def _exchange(client: TestClient, url: str, outgoing: Sequence[str]) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    with client.websocket_connect(url) as websocket:
        for raw in outgoing:
            websocket.send_text(raw)
            received.append(websocket.receive_json())
    return received


@pytest.mark.integration
async def test_message_turn_returns_reply_envelope(
    app: FastAPI, client: TestClient, db_engine: AsyncEngine, conversation_id: uuid.UUID
) -> None:
    _override_llm_client(app, FakeLLMClient())

    frames = await asyncio.to_thread(
        _exchange, client, f"/ws?conversation_id={conversation_id}", [_message("привет")]
    )

    assert frames == [{"type": "reply", "payload": {"role": "assistant", "content": "готово"}}]
    assert await _history(db_engine, conversation_id) == [
        ChatMessage(role="user", content="привет"),
        ChatMessage(role="assistant", content="готово"),
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "broken",
    ["не json", json.dumps({"payload": {"content": "привет"}}), json.dumps({"type": "message"})],
)
async def test_malformed_message_keeps_the_connection_usable(
    app: FastAPI, client: TestClient, conversation_id: uuid.UUID, broken: str
) -> None:
    _override_llm_client(app, FakeLLMClient())

    frames = await asyncio.to_thread(
        _exchange,
        client,
        f"/ws?conversation_id={conversation_id}",
        [broken, _message("привет")],
    )

    assert frames[0]["type"] == "error"
    assert frames[0]["payload"]["error"] == "ValidationError"
    assert frames[0]["payload"]["code"] == 422
    assert frames[1] == {"type": "reply", "payload": {"role": "assistant", "content": "готово"}}


@pytest.mark.integration
async def test_llm_failure_is_reported_without_closing_the_connection(
    app: FastAPI, client: TestClient, db_engine: AsyncEngine, conversation_id: uuid.UUID
) -> None:
    _override_llm_client(app, FakeLLMClient([LLMTransientError(), OK_RESPONSE]))

    frames = await asyncio.to_thread(
        _exchange,
        client,
        f"/ws?conversation_id={conversation_id}",
        [_message("упадёт"), _message("получится")],
    )

    assert frames[0] == {
        "type": "error",
        "payload": {
            "error": "LLMTransientError",
            "detail": LLMTransientError.default_message,
            "code": 503,
        },
    }
    assert frames[1] == {"type": "reply", "payload": {"role": "assistant", "content": "готово"}}

    assert await _history(db_engine, conversation_id) == [
        ChatMessage(role="user", content="получится"),
        ChatMessage(role="assistant", content="готово"),
    ]


@pytest.mark.integration
async def test_consecutive_messages_share_one_conversation_history(
    app: FastAPI, client: TestClient, db_engine: AsyncEngine, conversation_id: uuid.UUID
) -> None:
    llm_client = FakeLLMClient()
    _override_llm_client(app, llm_client)

    frames = await asyncio.to_thread(
        _exchange,
        client,
        f"/ws?conversation_id={conversation_id}",
        [_message("первый"), _message("второй")],
    )

    assert [frame["type"] for frame in frames] == ["reply", "reply"]
    assert llm_client.calls == [
        [get_system_prompt_message(), ChatMessage(role="user", content="первый")],
        [
            get_system_prompt_message(),
            ChatMessage(role="user", content="первый"),
            ChatMessage(role="assistant", content="готово"),
            ChatMessage(role="user", content="второй"),
        ],
    ]
    assert len(await _history(db_engine, conversation_id)) == 4


@pytest.mark.integration
async def test_unknown_conversation_id_is_rejected_on_connect(
    app: FastAPI, client: TestClient, db_engine: AsyncEngine
) -> None:
    _override_llm_client(app, FakeLLMClient())

    def _connect() -> tuple[dict[str, Any], bool]:
        with client.websocket_connect(f"/ws?conversation_id={uuid.uuid4()}") as websocket:
            frame = websocket.receive_json()
            try:
                websocket.receive_json()
            except WebSocketDisconnect:
                return frame, True
            return frame, False

    frame, closed = await asyncio.to_thread(_connect)

    assert frame == {
        "type": "error",
        "payload": {"error": "NotFoundError", "detail": "Ресурс не найден", "code": 404},
    }
    assert closed


@pytest.mark.integration
async def test_malformed_conversation_id_is_rejected_before_handshake(
    app: FastAPI, client: TestClient
) -> None:
    _override_llm_client(app, FakeLLMClient())

    def _connect() -> int:
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/ws?conversation_id=not-a-uuid"),
        ):
            pass
        return exc_info.value.code

    code = await asyncio.to_thread(_connect)

    assert code == status.WS_1008_POLICY_VIOLATION


@pytest.mark.integration
async def test_connection_without_conversation_id_uses_the_default_conversation(
    app: FastAPI, client: TestClient, db_engine: AsyncEngine
) -> None:
    _override_llm_client(app, FakeLLMClient())
    default_id: uuid.UUID | None = None

    try:
        frames = await asyncio.to_thread(_exchange, client, "/ws", [_message("привет")])
        assert frames == [{"type": "reply", "payload": {"role": "assistant", "content": "готово"}}]

        async with AsyncSession(db_engine, expire_on_commit=False) as session:
            stmt = select(Message.conversation_id).where(Message.content == "привет")
            default_id = (await session.execute(stmt)).scalars().one()
            assert (
                await ConversationRepository(session).get_or_create_default_conversation()
                == default_id
            )

        assert await _history(db_engine, default_id) == [
            ChatMessage(role="user", content="привет"),
            ChatMessage(role="assistant", content="готово"),
        ]
    finally:
        if default_id is not None:
            await _delete_conversation(db_engine, default_id)
