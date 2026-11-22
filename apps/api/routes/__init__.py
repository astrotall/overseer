from fastapi import APIRouter, Depends

from apps.api.auth import require_api_token
from apps.api.routes import confirmations, conversations, health, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(conversations.router, dependencies=[Depends(require_api_token)])
api_router.include_router(confirmations.router, dependencies=[Depends(require_api_token)])
api_router.include_router(ws.router)

__all__ = ["api_router"]
