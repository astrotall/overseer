from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from libs.schemas.chat import MessageResponse, SendMessageRequest
from libs.schemas.common import ErrorResponse


class WSIncomingMessage(BaseModel):
    type: Literal["message"]
    payload: SendMessageRequest


class WSReplyMessage(BaseModel):
    type: Literal["reply"] = "reply"
    payload: MessageResponse


class WSErrorPayload(ErrorResponse):
    code: int


class WSErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    payload: WSErrorPayload


WSServerMessage = Annotated[WSReplyMessage | WSErrorMessage, Field(discriminator="type")]
