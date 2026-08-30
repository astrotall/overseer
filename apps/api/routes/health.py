from __future__ import annotations

from fastapi import APIRouter

from libs.schemas.health import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Проверка живости сервиса")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")
