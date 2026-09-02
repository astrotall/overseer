from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from libs.core.exceptions import LLMBadRequestError, LLMError, LLMResponseError, LLMTransientError
from libs.schemas.common import ErrorResponse

LLM_ERROR_STATUS_CODES: dict[type[LLMError], int] = {
    LLMTransientError: 503,
    LLMBadRequestError: 400,
    LLMResponseError: 502,
}


def llm_error_status_code(exc: LLMError) -> int:
    for exc_type, status_code in LLM_ERROR_STATUS_CODES.items():
        if isinstance(exc, exc_type):
            return status_code
    return 502


async def llm_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, LLMError)
    return JSONResponse(
        status_code=llm_error_status_code(exc),
        content=ErrorResponse(error=type(exc).__name__, detail=exc.message).model_dump(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    for exc_type in LLM_ERROR_STATUS_CODES:
        app.add_exception_handler(exc_type, llm_error_handler)
