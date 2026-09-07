from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from libs.schemas.chat import ConfirmationRequiredResponse, MessageResponse, SendMessageRequest
from libs.schemas.common import ErrorResponse


class WSIncomingMessage(BaseModel):
    type: Literal["message"]
    payload: SendMessageRequest


class WSReplyMessage(BaseModel):
    type: Literal["reply"] = "reply"
    payload: MessageResponse


class WSConfirmationRequiredMessage(BaseModel):
    type: Literal["confirmation_required"] = "confirmation_required"
    payload: ConfirmationRequiredResponse


class WSErrorPayload(ErrorResponse):
    code: int


class WSErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    payload: WSErrorPayload


WSServerMessage = Annotated[
    WSReplyMessage | WSConfirmationRequiredMessage | WSErrorMessage,
    Field(discriminator="type"),
]
