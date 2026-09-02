from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message

from apps.api.deps import ChatServiceDep, ConversationRepositoryDep, SessionDep
from apps.api.exception_handlers import llm_error_status_code
from apps.api.services import ChatService
from libs.core.exceptions import LLMError, NotFoundError, OverseerError
from libs.core.logging import get_logger
from libs.db.repositories import ConversationRepository
from libs.schemas.chat import MessageResponse
from libs.schemas.ws import WSErrorMessage, WSErrorPayload, WSIncomingMessage, WSReplyMessage

logger = get_logger(__name__)

router = APIRouter()


def _extract_text(message: Message) -> str:
    text = message.get("text")
    if isinstance(text, str):
        return text
    data = message.get("bytes")
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return ""


def _validation_detail(exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}" if location else error["msg"])
    return "; ".join(details)


async def _send_reply(websocket: WebSocket, payload: MessageResponse) -> None:
    await websocket.send_json(WSReplyMessage(payload=payload).model_dump())


async def _send_error(websocket: WebSocket, *, error: str, detail: str, code: int) -> None:
    envelope = WSErrorMessage(payload=WSErrorPayload(error=error, detail=detail, code=code))
    await websocket.send_json(envelope.model_dump())


async def _resolve_conversation_id(
    repository: ConversationRepository,
    session: AsyncSession,
    conversation_id: uuid.UUID | None,
) -> uuid.UUID | None:
    if conversation_id is None:
        resolved = await repository.get_or_create_default_conversation()
        await session.commit()
        return resolved

    if await repository.conversation_exists(conversation_id):
        return conversation_id
    return None


async def _handle_turn(
    websocket: WebSocket,
    chat_service: ChatService,
    conversation_id: uuid.UUID,
    raw: str,
) -> None:
    try:
        incoming = WSIncomingMessage.model_validate_json(raw)
    except ValidationError as exc:
        logger.info("ws.invalid_message", conversation_id=str(conversation_id))
        await _send_error(
            websocket,
            error="ValidationError",
            detail=_validation_detail(exc),
            code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
        return

    try:
        answer = await chat_service.send_message(conversation_id, incoming.payload.content)
    except LLMError as exc:
        logger.warning(
            "ws.turn_failed",
            conversation_id=str(conversation_id),
            error=type(exc).__name__,
        )
        await _send_error(
            websocket,
            error=type(exc).__name__,
            detail=exc.message,
            code=llm_error_status_code(exc),
        )
        return
    except Exception:
        logger.exception("ws.turn_failed", conversation_id=str(conversation_id))
        await _send_error(
            websocket,
            error="InternalError",
            detail=OverseerError.default_message,
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        return

    await _send_reply(websocket, MessageResponse(role=answer.role, content=answer.content))


@router.websocket("/ws/chat")
async def agent_ws(
    websocket: WebSocket,
    session: SessionDep,
    repository: ConversationRepositoryDep,
    chat_service: ChatServiceDep,
    conversation_id: uuid.UUID | None = None,
) -> None:
    await websocket.accept()

    resolved_id = await _resolve_conversation_id(repository, session, conversation_id)
    if resolved_id is None:
        await _send_error(
            websocket,
            error="NotFoundError",
            detail=NotFoundError.default_message,
            code=status.HTTP_404_NOT_FOUND,
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    logger.info("ws.connected", client=str(websocket.client), conversation_id=str(resolved_id))
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            await _handle_turn(websocket, chat_service, resolved_id, _extract_text(message))
    finally:
        logger.info(
            "ws.disconnected", client=str(websocket.client), conversation_id=str(resolved_id)
        )
