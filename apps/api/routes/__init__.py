from fastapi import APIRouter

from apps.api.routes import confirmations, conversations, health, ws

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(conversations.router)
api_router.include_router(confirmations.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
