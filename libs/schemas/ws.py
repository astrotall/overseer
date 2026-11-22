from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field

from libs.schemas.chat import ConfirmationRequiredResponse, MessageResponse, SendMessageRequest
from libs.schemas.common import ErrorResponse

WS_SUBPROTOCOL: Final[str] = "overseer.v1"
WS_TOKEN_SUBPROTOCOL_PREFIX: Final[str] = "overseer.token."


def ws_auth_subprotocols(token: str) -> list[str]:
    return [WS_SUBPROTOCOL, f"{WS_TOKEN_SUBPROTOCOL_PREFIX}{token}"]


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
