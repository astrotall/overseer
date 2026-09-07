from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_active_llm_client, get_confirmation_store
from libs.confirmations import ConfirmationStore, PendingConfirmation
from libs.core.exceptions import LLMBadRequestError, LLMResponseError, LLMTransientError
from libs.db.repositories import ConversationRepository
from libs.db.session import get_session
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolCall, ToolSpec
from libs.schemas.chat import MAX_MESSAGE_LENGTH
from libs.tools import Tool, ToolResult, get_tool_registry


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


class ScriptedLLMClient(LLMClient):
    """Отдаёт на каждый вызов `complete()` следующий заскриптованный ответ по очереди."""

    default_model = "fake-model"

    def __init__(self, outcomes: Sequence[LLMResponse]) -> None:
        self._outcomes = list(outcomes)
        self.received_messages: list[Sequence[ChatMessage]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.received_messages.append(messages)
        return self._outcomes.pop(0)


class BoomArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int


class BoomTool(Tool[BoomArguments]):
    """Инструмент для теста OVE-23: `_execute` всегда бросает исключение."""

    name = "boom_http"
    description = "Инструмент для проверки полного хода при падении инструмента внутри."
    arguments_model = BoomArguments

    async def _execute(self, arguments: BoomArguments) -> ToolResult:
        raise RuntimeError("буум изнутри инструмента")


class DangerousArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class DangerousTool(Tool[DangerousArguments]):
    name = "delete_file"
    description = "Удаляет файл — необратимое действие, требует подтверждения."
    arguments_model = DangerousArguments
    requires_confirmation = True

    def __init__(self) -> None:
        self.executed = False

    async def _execute(self, arguments: DangerousArguments) -> ToolResult:
        self.executed = True
        return ToolResult.ok(summary=f"Файл {arguments.path} удалён")


class RecordingConfirmationStore(ConfirmationStore):
    """Store без Redis: OVE-26 проверяет форму ответа транспорта, а не хранилище."""

    def __init__(self) -> None:
        self.pending: dict[uuid.UUID, PendingConfirmation] = {}

    async def create_pending(
        self,
        conversation_id: uuid.UUID,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
    ) -> uuid.UUID:
        confirmation_id = uuid.uuid4()
        self.pending[confirmation_id] = PendingConfirmation(
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            summary=summary,
        )
        return confirmation_id


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


@pytest.mark.integration
async def test_a_tool_that_raises_inside_a_full_http_turn_comes_back_as_a_reply(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    """OVE-23: инструмент падает внутри настоящего request/response-цикла (не в изоляции
    execute(), как в OVE-20, и не через echo-инструмент, как в OVE-22): HTTP-ответ обязан
    остаться 200, а в базе — сохраниться корректный tool_result с ошибкой."""
    _override_session(app, db_session)
    get_tool_registry().register(BoomTool())
    call = ToolCall(id="call-1", name="boom_http", arguments={"value": 1})
    llm_client = ScriptedLLMClient(
        [
            LLMResponse(
                model="fake-model",
                stop_reason="tool_use",
                text="сейчас попробую",
                tool_calls=[call],
            ),
            LLMResponse(model="fake-model", stop_reason="end_turn", text="не получилось"),
        ]
    )
    _override_llm_client(app, llm_client)
    repository = ConversationRepository(db_session)
    conversation_id = await repository.create_conversation()
    await db_session.commit()

    with structlog.testing.capture_logs([structlog.processors.format_exc_info]) as log_entries:
        response = await async_client.post(
            f"/conversations/{conversation_id}/messages", json={"content": "запусти boom_http"}
        )

    assert response.status_code == 200
    assert response.json() == {"role": "assistant", "content": "не получилось"}

    history = await repository.get_history(conversation_id)
    assert [message.role for message in history] == ["user", "assistant", "tool", "assistant"]
    tool_message = history[2]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.is_error is True
    assert tool_message.content is not None
    assert "boom_http" in tool_message.content
    assert "буум изнутри инструмента" not in tool_message.content

    assert len(llm_client.received_messages) == 2
    second_call_messages = llm_client.received_messages[1]
    sent_tool_message = second_call_messages[-1]
    assert sent_tool_message.role == "tool"
    assert sent_tool_message.tool_call_id == "call-1"
    assert sent_tool_message.is_error is True
    assert sent_tool_message.content is not None
    assert "буум изнутри инструмента" not in sent_tool_message.content

    failure_entries = [entry for entry in log_entries if entry["event"] == "tool.execution_failed"]
    assert len(failure_entries) == 1
    (failure_entry,) = failure_entries
    assert failure_entry["log_level"] == "error"
    assert failure_entry["tool"] == "boom_http"
    assert "Traceback (most recent call last)" in failure_entry["exception"]
    assert "RuntimeError: буум изнутри инструмента" in failure_entry["exception"]


@pytest.mark.integration
async def test_a_tool_that_requires_confirmation_pauses_the_http_turn(
    app: FastAPI, async_client: AsyncClient, db_session: AsyncSession
) -> None:
    """OVE-26: пауза хода — отдельная форма ответа (202 с confirmation_id и summary),
    а не обычный MessageResponse; в базе остаётся только сообщение пользователя."""
    _override_session(app, db_session)
    tool = DangerousTool()
    get_tool_registry().register(tool)
    store = RecordingConfirmationStore()
    _override_confirmation_store(app, store)
    call = ToolCall(id="call-1", name="delete_file", arguments={"path": "отчёт.docx"})
    _override_llm_client(
        app,
        ScriptedLLMClient(
            [
                LLMResponse(
                    model="fake-model",
                    stop_reason="tool_use",
                    text="сейчас удалю",
                    tool_calls=[call],
                )
            ]
        ),
    )
    repository = ConversationRepository(db_session)
    conversation_id = await repository.create_conversation()
    await db_session.commit()

    response = await async_client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "удали отчёт"}
    )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"confirmation_id", "summary"}
    assert tool.executed is False

    pending = store.pending[uuid.UUID(body["confirmation_id"])]
    assert pending.tool_call_id == "call-1"
    assert pending.tool_name == "delete_file"
    assert pending.arguments == {"path": "отчёт.docx"}
    assert body["summary"] == pending.summary

    assert await repository.get_history(conversation_id) == [
        ChatMessage(role="user", content="удали отчёт")
    ]
