from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_active_llm_client
from libs.core.exceptions import LLMBadRequestError, LLMResponseError, LLMTransientError
from libs.db.repositories import ConversationRepository
from libs.db.session import get_session
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from libs.schemas.chat import MAX_MESSAGE_LENGTH


class FakeLLMClient(LLMClient):
    default_model = "fake-model"

    def __init__(self, response: LLMResponse | None = None) -> None:
        self.response = response or LLMResponse(
            model="fake-model", stop_reason="end_turn", text="готово"
        )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        return self.response


class FailingLLMClient(LLMClient):
    default_model = "fake-model"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        raise self._exc


def _override_session(app: FastAPI, db_session: AsyncSession) -> None:
    async def _get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _get_session


def _override_llm_client(app: FastAPI, llm_client: LLMClient) -> None:
    def _get_llm_client() -> LLMClient:
        return llm_client

    app.dependency_overrides[get_active_llm_client] = _get_llm_client


@pytest.mark.integration
async def test_create_conversation_returns_id(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)

    response = await async_client.post("/conversations")

    assert response.status_code == 201
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    assert await ConversationRepository(db_session).conversation_exists(conversation_id)


@pytest.mark.integration
async def test_send_message_persists_turn_and_returns_answer(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FakeLLMClient())
    repository = ConversationRepository(db_session)
    conversation_id = await repository.create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "привет"}
    )

    assert response.status_code == 200
    assert response.json() == {"role": "assistant", "content": "готово"}

    history = await repository.get_history(conversation_id)
    assert history == [
        ChatMessage(role="user", content="привет"),
        ChatMessage(role="assistant", content="готово"),
    ]


@pytest.mark.integration
async def test_send_message_to_missing_conversation_returns_404(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FakeLLMClient())

    response = await async_client.post(
        f"/conversations/{uuid.uuid4()}/messages", json={"content": "привет"}
    )

    assert response.status_code == 404


@pytest.mark.integration
async def test_send_message_rejects_blank_content(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FakeLLMClient())
    conversation_id = await ConversationRepository(db_session).create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "   "}
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_send_message_rejects_content_over_max_length(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FakeLLMClient())
    conversation_id = await ConversationRepository(db_session).create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "a" * (MAX_MESSAGE_LENGTH + 1)},
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_send_message_accepts_content_at_max_length(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FakeLLMClient())
    conversation_id = await ConversationRepository(db_session).create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "a" * MAX_MESSAGE_LENGTH},
    )

    assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (LLMTransientError(), 503),
        (LLMBadRequestError(), 400),
        (LLMResponseError(), 502),
    ],
)
async def test_llm_failures_map_to_http_status(
    app: FastAPI,
    async_client: AsyncClient,
    db_session: AsyncSession,
    exc: Exception,
    expected_status: int,
) -> None:
    _override_session(app, db_session)
    _override_llm_client(app, FailingLLMClient(exc))
    conversation_id = await ConversationRepository(db_session).create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "привет"}
    )

    assert response.status_code == expected_status
