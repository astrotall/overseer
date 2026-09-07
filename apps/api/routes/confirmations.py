from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status

from apps.api.deps import ConfirmationServiceDep
from apps.api.exception_handlers import CONFIRMATION_REQUIRED_STATUS_CODE
from libs.core.exceptions import ConflictError, NotFoundError
from libs.llm.base import ChatMessage
from libs.schemas.chat import ConfirmationRequiredResponse, MessageResponse

router = APIRouter(prefix="/confirmations", tags=["chat"])

_RESUME_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": "Подтверждение не найдено, истекло или уже разрешено",
    },
    status.HTTP_409_CONFLICT: {
        "description": "Подтверждение уже обрабатывается другим запросом",
    },
    CONFIRMATION_REQUIRED_STATUS_CODE: {
        "model": ConfirmationRequiredResponse,
        "description": "Возобновлённый ход снова приостановлен: нужно ещё одно подтверждение",
    },
}


@router.post(
    "/{confirmation_id}/confirm",
    response_model=MessageResponse,
    responses=_RESUME_RESPONSES,
    summary="Подтвердить отложенное действие и продолжить ход",
)
async def confirm(
    confirmation_id: uuid.UUID, confirmation_service: ConfirmationServiceDep
) -> MessageResponse:
    answer = await _resolve(confirmation_service, confirmation_id, approved=True)
    return MessageResponse(role=answer.role, content=answer.content)


@router.post(
    "/{confirmation_id}/reject",
    response_model=MessageResponse,
    responses=_RESUME_RESPONSES,
    summary="Отклонить отложенное действие и продолжить ход",
)
async def reject(
    confirmation_id: uuid.UUID, confirmation_service: ConfirmationServiceDep
) -> MessageResponse:
    answer = await _resolve(confirmation_service, confirmation_id, approved=False)
    return MessageResponse(role=answer.role, content=answer.content)


async def _resolve(
    confirmation_service: ConfirmationServiceDep,
    confirmation_id: uuid.UUID,
    *,
    approved: bool,
) -> ChatMessage:
    try:
        if approved:
            return await confirmation_service.confirm(confirmation_id)
        return await confirmation_service.reject(confirmation_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
