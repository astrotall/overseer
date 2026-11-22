from __future__ import annotations

import asyncio
import logging
import re
import secrets
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import httpx
import pytest
import structlog
import uvicorn
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.testclient import WebSocketDenialResponse
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Origin, Subprotocol

from apps.api.auth import FORBIDDEN_ORIGIN_DETAIL, UNAUTHORIZED_DETAIL
from apps.api.deps import (
    get_active_llm_client,
    get_confirmation_service,
    get_conversation_repository,
)
from libs.core.config import get_settings
from libs.core.exceptions import NotFoundError
from libs.core.logging import install_secret_redaction
from libs.db.session import get_session
from libs.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from libs.schemas.ws import WS_SUBPROTOCOL, WS_TOKEN_SUBPROTOCOL_PREFIX, ws_auth_subprotocols
from tests.conftest import (
    AUTH_HEADERS,
    TEST_API_TOKEN,
    WS_AUTH_SUBPROTOCOLS,
    authenticated_settings,
)

WRONG_TOKEN = "overseer-wrong-token-not-a-secret-012345678"
ALLOWED_ORIGIN = "tauri://localhost"
FOREIGN_ORIGIN = "https://evil.example"
PROTECTED_REQUESTS = [
    ("POST", "/conversations"),
    ("POST", f"/conversations/{uuid.UUID(int=1)}/messages"),
    ("POST", f"/confirmations/{uuid.UUID(int=2)}/confirm"),
    ("POST", f"/confirmations/{uuid.UUID(int=3)}/reject"),
]


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


class FakeRepository:
    def __init__(self) -> None:
        self.conversation_id = uuid.uuid4()

    async def create_conversation(self) -> uuid.UUID:
        return self.conversation_id

    async def get_or_create_default_conversation(self) -> uuid.UUID:
        return self.conversation_id

    async def conversation_exists(self, conversation_id: uuid.UUID) -> bool:
        return conversation_id == self.conversation_id


class NoConfirmationService:
    async def confirm(self, confirmation_id: uuid.UUID) -> ChatMessage:
        raise NotFoundError("нет такого подтверждения")

    async def reject(self, confirmation_id: uuid.UUID) -> ChatMessage:
        raise NotFoundError("нет такого подтверждения")


class SilentLLMClient(LLMClient):
    default_model = "fake-model"

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        raise AssertionError("тест не ожидал похода в LLM")


class SessionRecorder:
    def __init__(self) -> None:
        self.opened = 0
        self.session = FakeSession()

    async def __call__(self) -> AsyncIterator[FakeSession]:
        self.opened += 1
        yield self.session


@pytest.fixture
def sessions(app: FastAPI) -> SessionRecorder:
    recorder = SessionRecorder()
    repository = FakeRepository()
    app.dependency_overrides[get_session] = recorder
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    app.dependency_overrides[get_active_llm_client] = SilentLLMClient
    app.dependency_overrides[get_confirmation_service] = NoConfirmationService
    return recorder


@pytest.fixture
def ws_client(app: FastAPI, sessions: SessionRecorder) -> TestClient:
    return TestClient(app)


def _deny_status(client: TestClient, **kwargs: Any) -> WebSocketDenialResponse:
    with (
        pytest.raises(WebSocketDenialResponse) as denial,
        client.websocket_connect("/ws/chat", **kwargs),
    ):
        pass
    return denial.value


@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
async def test_a_request_without_a_token_is_refused(
    anonymous_client: AsyncClient, sessions: SessionRecorder, method: str, path: str
) -> None:
    response = await anonymous_client.request(method, path, json={"content": "привет"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": UNAUTHORIZED_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert sessions.opened == 0


@pytest.mark.parametrize(
    "authorization",
    [
        f"Bearer {WRONG_TOKEN}",
        f"Bearer {TEST_API_TOKEN[:-1]}",
        f"Bearer {TEST_API_TOKEN}x",
        f"Basic {TEST_API_TOKEN}",
        TEST_API_TOKEN,
        "Bearer ",
        "Bearer ключ".encode(),
    ],
)
@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
async def test_a_wrong_token_gets_the_same_answer_as_a_missing_one(
    anonymous_client: AsyncClient,
    sessions: SessionRecorder,
    method: str,
    path: str,
    authorization: str | bytes,
) -> None:
    response = await anonymous_client.request(
        method, path, json={"content": "привет"}, headers={"Authorization": authorization}
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json() == {"detail": UNAUTHORIZED_DETAIL}
    assert sessions.opened == 0


async def test_the_token_lets_a_conversation_be_created(
    async_client: AsyncClient, sessions: SessionRecorder
) -> None:
    response = await async_client.post("/conversations")

    assert response.status_code == status.HTTP_201_CREATED
    assert sessions.session.commits == 1


async def test_the_token_lets_a_confirmation_be_answered(
    async_client: AsyncClient, sessions: SessionRecorder
) -> None:
    response = await async_client.post(f"/confirmations/{uuid.uuid4()}/confirm")

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_the_bearer_scheme_is_case_insensitive(
    anonymous_client: AsyncClient, sessions: SessionRecorder
) -> None:
    response = await anonymous_client.post(
        "/conversations", headers={"Authorization": f"bearer {TEST_API_TOKEN}"}
    )

    assert response.status_code == status.HTTP_201_CREATED


async def test_health_is_open_and_says_nothing_else(anonymous_client: AsyncClient) -> None:
    response = await anonymous_client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


async def test_a_server_without_a_token_refuses_everyone(
    app: FastAPI, anonymous_client: AsyncClient, sessions: SessionRecorder
) -> None:
    app.dependency_overrides[get_settings] = lambda: authenticated_settings(api_token=None)

    response = await anonymous_client.post("/conversations", headers=AUTH_HEADERS)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_every_http_route_but_health_requires_authentication(
    app: FastAPI, anonymous_client: AsyncClient, sessions: SessionRecorder
) -> None:
    checked: set[str] = set()
    for path, operations in app.openapi()["paths"].items():
        concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        for method in operations:
            response = await anonymous_client.request(method.upper(), concrete, json={})
            expected = status.HTTP_200_OK if path == "/health" else status.HTTP_401_UNAUTHORIZED
            assert response.status_code == expected, f"{method.upper()} {path}"
            checked.add(f"{method.upper()} {path}")

    assert checked >= {
        "GET /health",
        "POST /conversations",
        "POST /conversations/{conversation_id}/messages",
        "POST /confirmations/{confirmation_id}/confirm",
        "POST /confirmations/{confirmation_id}/reject",
    }
    assert sessions.opened == 0


async def test_the_token_is_compared_in_constant_time(
    async_client: AsyncClient, sessions: SessionRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = secrets.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr("apps.api.auth.secrets.compare_digest", spy)

    response = await async_client.post("/conversations")

    assert response.status_code == status.HTTP_201_CREATED
    assert calls == [(TEST_API_TOKEN.encode(), TEST_API_TOKEN.encode())]


def test_a_socket_without_a_token_is_refused_before_the_handshake(
    ws_client: TestClient, sessions: SessionRecorder
) -> None:
    denial = _deny_status(ws_client)

    assert denial.status_code == status.HTTP_401_UNAUTHORIZED
    assert denial.json()["detail"] == UNAUTHORIZED_DETAIL
    assert sessions.opened == 0


@pytest.mark.parametrize(
    "subprotocols",
    [
        ws_auth_subprotocols(WRONG_TOKEN),
        [f"{WS_TOKEN_SUBPROTOCOL_PREFIX}{TEST_API_TOKEN}"],
        [WS_SUBPROTOCOL],
        [
            WS_SUBPROTOCOL,
            f"{WS_TOKEN_SUBPROTOCOL_PREFIX}{TEST_API_TOKEN}",
            f"{WS_TOKEN_SUBPROTOCOL_PREFIX}{WRONG_TOKEN}",
        ],
    ],
    ids=["wrong-token", "no-protocol-name", "no-token", "two-tokens"],
)
def test_a_socket_with_a_bad_token_is_refused(
    ws_client: TestClient, sessions: SessionRecorder, subprotocols: list[str]
) -> None:
    denial = _deny_status(ws_client, subprotocols=subprotocols)

    assert denial.status_code == status.HTTP_401_UNAUTHORIZED
    assert sessions.opened == 0


@pytest.mark.parametrize("origin", [FOREIGN_ORIGIN, "null", "http://127.0.0.1:8000"])
def test_a_foreign_origin_is_refused_even_with_a_valid_token(
    ws_client: TestClient, sessions: SessionRecorder, origin: str
) -> None:
    denial = _deny_status(ws_client, subprotocols=WS_AUTH_SUBPROTOCOLS, headers={"Origin": origin})

    assert denial.status_code == status.HTTP_403_FORBIDDEN
    assert denial.json()["detail"] == FORBIDDEN_ORIGIN_DETAIL
    assert sessions.opened == 0


def test_an_allowed_origin_still_needs_the_token(
    app: FastAPI, ws_client: TestClient, sessions: SessionRecorder
) -> None:
    app.dependency_overrides[get_settings] = lambda: authenticated_settings(
        api_allowed_origins=[ALLOWED_ORIGIN]
    )

    denial = _deny_status(ws_client, headers={"Origin": ALLOWED_ORIGIN})

    assert denial.status_code == status.HTTP_401_UNAUTHORIZED
    assert sessions.opened == 0


def test_a_native_client_with_the_token_and_no_origin_is_accepted(
    ws_client: TestClient, sessions: SessionRecorder
) -> None:
    with ws_client.websocket_connect("/ws/chat", subprotocols=WS_AUTH_SUBPROTOCOLS) as websocket:
        assert websocket.accepted_subprotocol == WS_SUBPROTOCOL

    assert sessions.opened == 1


def test_an_allowed_origin_with_the_token_is_accepted(
    app: FastAPI, ws_client: TestClient, sessions: SessionRecorder
) -> None:
    app.dependency_overrides[get_settings] = lambda: authenticated_settings(
        api_allowed_origins=[ALLOWED_ORIGIN]
    )

    with ws_client.websocket_connect(
        "/ws/chat", subprotocols=WS_AUTH_SUBPROTOCOLS, headers={"Origin": ALLOWED_ORIGIN}
    ) as websocket:
        assert websocket.accepted_subprotocol == WS_SUBPROTOCOL

    assert sessions.opened == 1


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def live_server(app: FastAPI, sessions: SessionRecorder) -> Iterator[str]:
    install_secret_redaction()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        threading.Event().wait(0.01)
    assert server.started, "uvicorn не поднялся"
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_the_token_never_reaches_any_log(
    live_server: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token_protocols = [Subprotocol(value) for value in WS_AUTH_SUBPROTOCOLS]
    wrong_protocols = [Subprotocol(value) for value in ws_auth_subprotocols(WRONG_TOKEN)]

    with structlog.testing.capture_logs() as events:
        async with connect(f"ws://{live_server}/ws/chat", subprotocols=token_protocols) as ws:
            assert ws.subprotocol == WS_SUBPROTOCOL

        with pytest.raises(InvalidStatus) as refused:
            await connect(f"ws://{live_server}/ws/chat", subprotocols=wrong_protocols)
        assert refused.value.response.status_code == status.HTTP_401_UNAUTHORIZED

        with pytest.raises(InvalidStatus) as foreign:
            await connect(
                f"ws://{live_server}/ws/chat",
                subprotocols=token_protocols,
                origin=Origin(FOREIGN_ORIGIN),
            )
        assert foreign.value.response.status_code == status.HTTP_403_FORBIDDEN

        async with httpx.AsyncClient(base_url=f"http://{live_server}") as http:
            assert (await http.post("/conversations", headers=AUTH_HEADERS)).status_code == 201
            wrong = {"Authorization": f"Bearer {WRONG_TOKEN}"}
            assert (await http.post("/conversations", headers=wrong)).status_code == 401

        await asyncio.sleep(0.05)

    access_lines = [record.getMessage() for record in caplog.records]
    assert any('"WebSocket /ws/chat" 401' in line for line in access_lines)
    assert any('"WebSocket /ws/chat" [accepted]' in line for line in access_lines)
    assert any(event["event"] == "ws.access_denied" for event in events)
    everything = repr(access_lines) + repr(events) + caplog.text
    assert TEST_API_TOKEN not in everything
    assert WRONG_TOKEN not in everything
