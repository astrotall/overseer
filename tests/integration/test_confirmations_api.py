from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_active_llm_client, get_confirmation_store
from libs.confirmations import ConfirmationStore
from libs.db.repositories import ConversationRepository
from libs.db.session import get_session
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.tools import EchoTool, Tool, ToolResult, get_tool_registry


class ScriptedLLMClient(LLMClient):
    """Отдаёт на каждый вызов `complete()` следующий заскриптованный ответ по очереди."""

    default_model = "fake-model"

    def __init__(self, outcomes: Sequence[LLMResponse]) -> None:
        self._outcomes = list(outcomes)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        return self._outcomes.pop(0)


class DangerousArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class DangerousTool(Tool[DangerousArguments]):
    name = "delete_file"
    description = "Удаляет файл — необратимое действие, требует подтверждения."
    arguments_model = DangerousArguments
    requires_confirmation = True

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def _execute(self, arguments: DangerousArguments) -> ToolResult:
        self.deleted.append(arguments.path)
        return ToolResult.ok(summary=f"Файл {arguments.path} удалён")


DELETE_CALL = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})


def _tool_use(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(model="fake-model", stop_reason="tool_use", tool_calls=list(calls))


def _final(text: str) -> LLMResponse:
    return LLMResponse(model="fake-model", stop_reason="end_turn", text=text)


def _override_session(app: FastAPI, db_session: AsyncSession) -> None:
    async def _get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _get_session


def _override_llm_client(app: FastAPI, llm_client: LLMClient) -> None:
    def _get_llm_client() -> LLMClient:
        return llm_client

    app.dependency_overrides[get_active_llm_client] = _get_llm_client


def _override_confirmation_store(app: FastAPI, store: ConfirmationStore) -> None:
    def _get_confirmation_store() -> ConfirmationStore:
        return store

    app.dependency_overrides[get_confirmation_store] = _get_confirmation_store


async def _pause_on_delete(
    app: FastAPI,
    async_client: AsyncClient,
    db_session: AsyncSession,
    outcomes: Sequence[LLMResponse],
    *tools: Tool[Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    _override_session(app, db_session)
    _override_llm_client(app, ScriptedLLMClient([_tool_use(DELETE_CALL), *outcomes]))
    for tool in tools:
        get_tool_registry().register(tool)

    conversation_id = await ConversationRepository(db_session).create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "удали отчёт"}
    )
    assert response.status_code == 202
    return conversation_id, uuid.UUID(response.json()["confirmation_id"])


@pytest.mark.integration
async def test_confirm_runs_the_tool_and_returns_the_finished_answer(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    _override_confirmation_store(app, store)
    tool = DangerousTool()
    conversation_id, confirmation_id = await _pause_on_delete(
        app, async_client, db_session, [_final("Файл удалён.")], tool
    )

    response = await async_client.post(f"/confirmations/{confirmation_id}/confirm")

    assert response.status_code == 200
    assert response.json() == {"role": "assistant", "content": "Файл удалён."}
    assert tool.deleted == ["отчёт.docx"]

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.integration
async def test_reject_returns_the_answer_without_running_the_tool(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    _override_confirmation_store(app, store)
    tool = DangerousTool()
    conversation_id, confirmation_id = await _pause_on_delete(
        app, async_client, db_session, [_final("Хорошо, не удаляю.")], tool
    )

    response = await async_client.post(f"/confirmations/{confirmation_id}/reject")

    assert response.status_code == 200
    assert response.json() == {"role": "assistant", "content": "Хорошо, не удаляю."}
    assert tool.deleted == []

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert history[2].role == "tool"
    assert history[2].is_error is True


@pytest.mark.integration
async def test_confirming_the_same_id_twice_answers_404(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    _override_confirmation_store(app, store)
    tool = DangerousTool()
    _, confirmation_id = await _pause_on_delete(
        app, async_client, db_session, [_final("Файл удалён.")], tool
    )

    assert (await async_client.post(f"/confirmations/{confirmation_id}/confirm")).status_code == 200

    response = await async_client.post(f"/confirmations/{confirmation_id}/confirm")

    assert response.status_code == 404
    assert tool.deleted == ["отчёт.docx"]


@pytest.mark.integration
async def test_an_unknown_confirmation_answers_404(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    _override_session(app, db_session)
    _override_confirmation_store(app, ConfirmationStore(redis_client))

    response = await async_client.post(f"/confirmations/{uuid.uuid4()}/reject")

    assert response.status_code == 404


@pytest.mark.integration
async def test_a_continuation_that_needs_another_confirmation_answers_202(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    _override_confirmation_store(app, store)
    tool = DangerousTool()
    second_call = ToolCall(id="call-2", name="delete_file", arguments={"path": "черновик.docx"})
    _, confirmation_id = await _pause_on_delete(
        app, async_client, db_session, [_tool_use(second_call)], tool
    )

    response = await async_client.post(f"/confirmations/{confirmation_id}/confirm")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"confirmation_id", "summary"}
    assert "черновик.docx" in body["summary"]
    assert tool.deleted == ["отчёт.docx"]

    second_id = uuid.UUID(body["confirmation_id"])
    assert second_id != confirmation_id
    assert (await store.get_pending(second_id)).tool_call_id == "call-2"
    await store.resolve_pending(second_id)


@pytest.mark.integration
async def test_the_continuation_may_call_an_ordinary_tool_over_http(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    store = ConfirmationStore(redis_client)
    _override_confirmation_store(app, store)
    tool = DangerousTool()
    echo_call = ToolCall(id="call-2", name="echo", arguments={"text": "готово"})
    conversation_id, confirmation_id = await _pause_on_delete(
        app,
        async_client,
        db_session,
        [_tool_use(echo_call), _final("Удалил и повторил.")],
        tool,
        EchoTool(),
    )

    response = await async_client.post(f"/confirmations/{confirmation_id}/confirm")

    assert response.status_code == 200
    assert response.json()["content"] == "Удалил и повторил."

    history = await ConversationRepository(db_session).get_history(conversation_id)
    assert [message.role for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [message.tool_call_id for message in (history[2], history[4])] == ["call-1", "call-2"]
