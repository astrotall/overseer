from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from apps.api.deps import ChatServiceDep, ConversationRepositoryDep, SessionDep
from libs.schemas.chat import CreateConversationResponse, MessageResponse, SendMessageRequest

router = APIRouter(prefix="/conversations", tags=["chat"])


@router.post(
    "",
    response_model=CreateConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать новый диалог",
)
async def create_conversation(
    session: SessionDep, repository: ConversationRepositoryDep
) -> CreateConversationResponse:
    conversation_id = await repository.create_conversation()
    await session.commit()
    return CreateConversationResponse(conversation_id=conversation_id)


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageResponse,
    summary="Отправить сообщение в диалог и получить ответ ассистента",
)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    repository: ConversationRepositoryDep,
    chat_service: ChatServiceDep,
) -> MessageResponse:
    if not await repository.conversation_exists(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Диалог не найден")

    answer = await chat_service.send_message(conversation_id, payload.content)
    return MessageResponse(role=answer.role, content=answer.content)
