from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, TypeVar

import httpx
from pydantic import ValidationError

from apps.voice.config import VoiceSettings
from libs.core.exceptions import ExternalServiceError
from libs.core.logging import get_logger
from libs.schemas.chat import ConfirmationRequiredResponse, MessageResponse

logger = get_logger(__name__)

PayloadT = TypeVar("PayloadT", ConfirmationRequiredResponse, MessageResponse)

REQUEST_TIMEOUT_S: Final[float] = 120.0
CONFIRM_PATH: Final[str] = "/confirmations/{confirmation_id}/confirm"
REJECT_PATH: Final[str] = "/confirmations/{confirmation_id}/reject"

WORD: Final[re.Pattern[str]] = re.compile(r"[^\W\d_]+")

CONFIRM_WORDS: Final[frozenset[str]] = frozenset(
    {
        "да",
        "ага",
        "угу",
        "конечно",
        "давай",
        "давайте",
        "ок",
        "окей",
        "хорошо",
        "подтверждаю",
        "подтверждай",
        "согласен",
        "согласна",
        "разрешаю",
        "валяй",
        "выполняй",
        "делай",
        "продолжай",
        "yes",
        "ok",
        "okay",
    }
)
REJECT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "нет",
        "не",
        "неа",
        "отмена",
        "отмени",
        "отменяй",
        "отклони",
        "отклоняю",
        "отставить",
        "стоп",
        "останови",
        "прекрати",
        "no",
        "cancel",
        "stop",
    }
)


class Decision(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    UNCLEAR = "unclear"


def classify(text: str) -> Decision:
    words = {word.lower() for word in WORD.findall(text)}
    confirming = bool(words & CONFIRM_WORDS)
    rejecting = bool(words & REJECT_WORDS)
    if confirming is rejecting:
        return Decision.UNCLEAR

    return Decision.CONFIRM if confirming else Decision.REJECT


@dataclass(frozen=True, slots=True)
class Resolution:
    reply: str | None = None
    pending: ConfirmationRequiredResponse | None = None


class ConfirmationError(ExternalServiceError):
    default_message = "Не удалось передать ответ на подтверждение"


class ConfirmationGoneError(ConfirmationError):
    default_message = "Подтверждение больше не действует"


class ConfirmationAPI(Protocol):
    async def resolve(self, confirmation_id: uuid.UUID, *, approved: bool) -> Resolution: ...


class HTTPConfirmationAPI:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = REQUEST_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: VoiceSettings) -> HTTPConfirmationAPI:
        return cls(settings.api_base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve(self, confirmation_id: uuid.UUID, *, approved: bool) -> Resolution:
        path = CONFIRM_PATH if approved else REJECT_PATH
        url = path.format(confirmation_id=confirmation_id)
        try:
            response = await self._client.post(url)
        except httpx.HTTPError as exc:
            logger.warning(
                "voice.confirmation_request_failed",
                confirmation_id=str(confirmation_id),
                approved=approved,
                error=type(exc).__name__,
                detail=str(exc),
            )
            raise ConfirmationError(f"Запрос к {url} не дошёл: {type(exc).__name__}") from exc

        return self._read(response, confirmation_id, approved=approved)

    def _read(
        self, response: httpx.Response, confirmation_id: uuid.UUID, *, approved: bool
    ) -> Resolution:
        logger.info(
            "voice.confirmation_resolved",
            confirmation_id=str(confirmation_id),
            approved=approved,
            status=response.status_code,
        )
        if response.status_code == httpx.codes.ACCEPTED:
            return Resolution(pending=self._parse(response, ConfirmationRequiredResponse))
        if response.status_code == httpx.codes.OK:
            return Resolution(reply=self._parse(response, MessageResponse).content)
        if response.status_code in {httpx.codes.NOT_FOUND, httpx.codes.CONFLICT}:
            raise ConfirmationGoneError(
                f"Подтверждение {confirmation_id} не принято: {response.status_code}"
            )

        raise ConfirmationError(f"Неожиданный ответ {response.status_code} на {response.url.path}")

    def _parse(self, response: httpx.Response, model: type[PayloadT]) -> PayloadT:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise ConfirmationError(
                f"Ответ {response.status_code} не разбирается как {model.__name__}"
            ) from exc
