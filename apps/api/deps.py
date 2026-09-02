from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.services import ChatService
from libs.core.config import Settings, get_settings
from libs.db.redis import get_redis
from libs.db.session import get_session
from libs.llm.base import LLMClient
from libs.llm.factory import get_llm_client

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_active_llm_client(settings: SettingsDep) -> LLMClient:
    return get_llm_client(settings)


LLMClientDep = Annotated[LLMClient, Depends(get_active_llm_client)]


def get_chat_service(session: SessionDep, llm_client: LLMClientDep) -> ChatService:
    return ChatService(session, llm_client)


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]

__all__ = [
    "ChatServiceDep",
    "LLMClientDep",
    "RedisDep",
    "SessionDep",
    "SettingsDep",
    "get_active_llm_client",
    "get_chat_service",
]
