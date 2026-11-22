from __future__ import annotations

import secrets
from typing import Final

from fastapi import HTTPException, Request, WebSocket, status
from fastapi.responses import JSONResponse

from apps.api.deps import SettingsDep
from libs.core.config import Settings
from libs.core.logging import get_logger
from libs.schemas.common import ErrorResponse
from libs.schemas.ws import WS_SUBPROTOCOL, WS_TOKEN_SUBPROTOCOL_PREFIX

logger = get_logger(__name__)

BEARER_PREFIX: Final[str] = "bearer "
UNAUTHORIZED_DETAIL: Final[str] = "Требуется аутентификация"
FORBIDDEN_ORIGIN_DETAIL: Final[str] = "Источник запроса не разрешён"
DENIALS: Final[dict[int, ErrorResponse]] = {
    status.HTTP_401_UNAUTHORIZED: ErrorResponse(error="Unauthorized", detail=UNAUTHORIZED_DETAIL),
    status.HTTP_403_FORBIDDEN: ErrorResponse(error="Forbidden", detail=FORBIDDEN_ORIGIN_DETAIL),
}
WS_DENIAL_EXTENSION: Final[str] = "websocket.http.response"


class WebSocketAccessDeniedError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(DENIALS[status_code].detail)
        self.status_code = status_code


def token_matches(presented: str | None, settings: Settings) -> bool:
    expected = settings.api_token
    if expected is None or not presented:
        return False
    return secrets.compare_digest(
        presented.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    )


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None or not authorization.lower().startswith(BEARER_PREFIX):
        return None
    return authorization[len(BEARER_PREFIX) :].strip()


def _subprotocol_token(subprotocols: list[str]) -> str | None:
    if WS_SUBPROTOCOL not in subprotocols:
        return None
    tokens = [
        value[len(WS_TOKEN_SUBPROTOCOL_PREFIX) :]
        for value in subprotocols
        if value.startswith(WS_TOKEN_SUBPROTOCOL_PREFIX)
    ]
    return tokens[0] if len(tokens) == 1 else None


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    return origin is None or origin.strip().lower() in settings.api_allowed_origins


async def require_api_token(request: Request, settings: SettingsDep) -> None:
    if token_matches(_bearer_token(request.headers.get("authorization")), settings):
        return

    logger.warning("api.unauthorized", path=request.url.path)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_ws_access(websocket: WebSocket, settings: SettingsDep) -> None:
    origin_ok = origin_allowed(websocket.headers.get("origin"), settings)
    subprotocols = list(websocket.scope.get("subprotocols", []))
    token_ok = token_matches(_subprotocol_token(subprotocols), settings)
    if origin_ok and token_ok:
        return

    status_code = status.HTTP_403_FORBIDDEN if not origin_ok else status.HTTP_401_UNAUTHORIZED
    logger.warning(
        "ws.access_denied",
        client=str(websocket.client),
        reason="origin" if not origin_ok else "token",
    )
    raise WebSocketAccessDeniedError(status_code)


async def websocket_access_denied_handler(websocket: WebSocket, exc: Exception) -> None:
    assert isinstance(exc, WebSocketAccessDeniedError)
    if WS_DENIAL_EXTENSION in websocket.scope.get("extensions", {}):
        denial = JSONResponse(
            status_code=exc.status_code, content=DENIALS[exc.status_code].model_dump()
        )
        await websocket.send_denial_response(denial)
        return

    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
